#!/usr/bin/env python3
"""Qwen Code adapter — session resolution, initialization, and garbage collection.

Owns Qwen-specific session logic; individual hook events are handled by
handlers.py. Modelled on tracing/claude_code/hooks/adapter.py, because Qwen
Code's hook payloads carry the same identifying fields (``session_id``, ``cwd``,
``transcript_path``).

Differences from Claude Code, verified against qwen 0.21.3:

* transcripts live under ``~/.qwen/projects/<encoded-cwd>/chats/<uuid>.jsonl``
  — Claude Code omits the ``chats`` segment;
* ``session_id`` is present on every observed event, so the PID fallback is a
  safety net rather than a routine path.
"""

import hashlib
import hmac
import json
import os
import platform
import re
import stat
import time
from pathlib import Path
from typing import Callable, Optional

from core.common import (
    FileLock,
    StateManager,
    _open_directory_no_symlinks,
    env,
    generate_trace_id,
    get_timestamp_ms,
    log,
)
from core.constants import HARNESSES, STATE_BASE_DIR
from tracing.qwen.constants import CHATS_SUBDIR, PROJECTS_DIR

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["qwen"]
SERVICE_NAME = _HARNESS["service_name"]  # "qwen"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-qwen-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/qwen
EXPLICIT_STATE_TTL_SECONDS = 7 * 24 * 60 * 60
LOCK_SHARD_COUNT = 256
_NAMESPACE_KEY_FILE = ".namespace_key"


def _state_namespace_key() -> bytes:
    """Return an owner-only local key used to hide guessable session paths."""
    key_path = STATE_DIR / _NAMESPACE_KEY_FILE
    with FileLock(STATE_DIR / f"{_NAMESPACE_KEY_FILE}.lock"):
        directory_fd = _open_directory_no_symlinks(STATE_DIR, create=False)
        try:
            if os.name != "nt":
                os.fchmod(directory_fd, 0o700)
            read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = (
                    os.open(key_path, read_flags)
                    if os.name == "nt"
                    else os.open(_NAMESPACE_KEY_FILE, read_flags, dir_fd=directory_fd)
                )
            except FileNotFoundError:
                key = os.urandom(32)
                temp_name = f".{_NAMESPACE_KEY_FILE}.{os.getpid()}.{os.urandom(8).hex()}"
                temp_path = STATE_DIR / temp_name
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
                fd = (
                    os.open(temp_path, flags, 0o600)
                    if os.name == "nt"
                    else os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
                )
                try:
                    view = memoryview(key)
                    while view:
                        written = os.write(fd, view)
                        if written <= 0:
                            raise OSError("short write while creating namespace key")
                        view = view[written:]
                    os.fsync(fd)
                finally:
                    os.close(fd)
                try:
                    if os.name == "nt":
                        os.replace(temp_path, key_path)
                    else:
                        os.replace(
                            temp_name,
                            _NAMESPACE_KEY_FILE,
                            src_dir_fd=directory_fd,
                            dst_dir_fd=directory_fd,
                        )
                        os.fsync(directory_fd)
                finally:
                    try:
                        if os.name == "nt":
                            temp_path.unlink()
                        else:
                            os.unlink(temp_name, dir_fd=directory_fd)
                    except FileNotFoundError:
                        pass
                fd = (
                    os.open(key_path, read_flags)
                    if os.name == "nt"
                    else os.open(_NAMESPACE_KEY_FILE, read_flags, dir_fd=directory_fd)
                )

            try:
                metadata = os.fstat(fd)
                if not stat.S_ISREG(metadata.st_mode):
                    raise OSError("namespace key is not a regular file")
                if os.name != "nt" and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
                    raise OSError("namespace key is not owner-only")
                key = b""
                while len(key) < 33:
                    chunk = os.read(fd, 33 - len(key))
                    if not chunk:
                        break
                    key += chunk
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
    if len(key) != 32:
        raise OSError("invalid Qwen namespace key")
    return key


def _keyed_session_digest(identity: str) -> str:
    return hmac.new(_state_namespace_key(), identity.encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def _lock_path_for_session_key(session_key: str) -> Path:
    lock_digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
    lock_shard = int(lock_digest[:8], 16) % LOCK_SHARD_COUNT
    return STATE_DIR / f".lock_shard_{lock_shard:03d}"


def _lifecycle_lock_for_state_file(state_file: Path) -> FileLock:
    session_key = state_file.stem.replace("state_", "", 1)
    base = _lock_path_for_session_key(session_key)
    return FileLock(base.with_name(f"{base.name}.lifecycle"), timeout=0.0, break_on_timeout=False)


def resolve_session(input_json: dict) -> StateManager:
    """Resolve the per-session state file from hook input JSON.

    Priority for the session key:

    1. ``input_json["session_id"]`` — present on every Qwen Code hook event;
    2. ``QWEN_SESSION_KEY`` env var;
    3. parent PID, as a last resort.

    Unlike Claude Code we do not reach for the *grandparent* PID: that heuristic
    encodes Claude's ``claude -> node -> hook`` process tree, which does not
    describe Qwen Code. Since ``session_id`` is always supplied, the PID branch
    only guards against malformed input.
    """
    session_key = None

    sid = input_json.get("session_id", "")
    if sid:
        cwd = str(input_json.get("cwd", "") or "")
        try:
            canonical_cwd = str(Path(cwd).expanduser().resolve()) if cwd else ""
            runtime_namespace = str(_runtime_base_dir(canonical_cwd or os.getcwd()))
        except (OSError, RuntimeError, ValueError):
            canonical_cwd = cwd
            runtime_namespace = os.environ.get("QWEN_RUNTIME_DIR", "")
        identity = f"{sid}\0{canonical_cwd}\0{runtime_namespace}"
        digest = _keyed_session_digest(identity)
        session_key = f"session_{digest}"

    if not session_key:
        env_key = os.environ.get("QWEN_SESSION_KEY", "")
        if env_key:
            digest = _keyed_session_digest(env_key)
            session_key = f"session_{digest}"

    if not session_key:
        ppid = os.getppid()
        session_key = f"pid_{ppid if ppid > 0 else os.getpid()}"

    state_file = STATE_DIR / f"state_{session_key}.json"
    lock_path = _lock_path_for_session_key(session_key)

    return StateManager(
        state_dir=STATE_DIR,
        state_file=state_file,
        lock_path=lock_path,
    )


def ensure_session_initialized(state: StateManager, input_json: dict) -> None:
    """Idempotent session initialization. No-op if session_id is already stored.

    Sets: ``session_id``, ``session_start_time``, ``project_name``,
    ``trace_count``, ``tool_count``, ``user_id`` and — when the payload carries
    it — ``model``.
    """
    existing = state.get("session_id")
    if existing is not None:
        return

    session_id = input_json.get("session_id", "")
    if not session_id:
        session_id = generate_trace_id()

    # project_name: framework-scoped env override > config.json > cwd basename.
    project_name = env.project_name_for(SERVICE_NAME)
    if not project_name and not (env.log_prompts or env.log_tool_details or env.log_tool_content):
        project_name = "[REDACTED]"
    if not project_name:
        cwd = input_json.get("cwd", "")
        project_name = os.path.basename(cwd) if cwd else os.path.basename(os.getcwd())

    initial = {
        "session_id": session_id,
        "session_start_time": str(get_timestamp_ms()),
        "project_name": project_name,
        "trace_count": "0",
        "tool_count": "0",
    }
    model = input_json.get("model", "")
    if model:
        initial["model"] = model

    user_id = env.get_user_id(SERVICE_NAME)
    if not user_id:
        user_id = input_json.get("user_id", "")
    initial["user_id"] = user_id
    if state.set_many(initial) is False:
        raise OSError("failed to initialize Qwen session state atomically")

    log(f"Session initialized: {session_id}")


def _has_symlink_below(anchor: Path, candidate: Path) -> bool:
    """Check lexical path components below a trusted anchor without following them."""
    anchor = Path(os.path.abspath(anchor.expanduser()))
    candidate = Path(os.path.abspath(candidate.expanduser()))
    try:
        relative = candidate.relative_to(anchor)
    except ValueError:
        return True
    current = anchor
    for component in relative.parts:
        current /= component
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def validate_transcript_path(
    path: Path,
    root: Optional[Path] = None,
    anchor: Optional[Path] = None,
    *,
    must_exist: bool = True,
) -> Optional[Path]:
    """Return a Qwen JSONL path confined beneath the requested root.

    Confinement and existence are separate questions. Qwen creates a session
    transcript only when it writes the first record, which happens *after*
    ``UserPromptSubmit`` fires, so on the opening turn the canonical path is
    legitimate but absent. Requiring existence there rejects it exactly as it
    would reject a substituted path, and the turn is never opened — in one-shot
    mode that means the whole session produces no spans.

    Callers that need a readable file keep ``must_exist=True``; the turn-opening
    path passes ``must_exist=False`` and defers the existence check to ``Stop``,
    by which time Qwen has written the transcript. Symlink, root-containment and
    suffix checks apply either way.
    """
    try:
        lexical_root = (root or PROJECTS_DIR).expanduser()
        lexical_anchor = (anchor or lexical_root).expanduser()
        if lexical_root.is_symlink() or _has_symlink_below(lexical_anchor, lexical_root):
            return None
        if _has_symlink_below(lexical_anchor, path):
            return None
        # Non-strict resolution still collapses symlinks in the existing
        # prefix, so containment holds for a path whose leaf is not there yet.
        allowed_root = lexical_root.resolve()
        candidate = path.expanduser().resolve(strict=must_exist)
        candidate.relative_to(allowed_root)
    except (OSError, RuntimeError, ValueError):
        return None
    if candidate.suffix != ".jsonl":
        return None
    if must_exist and not candidate.is_file():
        return None
    if not must_exist and candidate.exists() and not candidate.is_file():
        return None
    return candidate


def _read_qwen_settings(path: Path) -> dict:
    try:
        from tracing.qwen.install import _strip_json_comments, _strip_jsonc_trailing_commas

        text = path.read_text(encoding="utf-8").lstrip("\ufeff")
        normalized = _strip_jsonc_trailing_commas(_strip_json_comments(text))
        settings = json.loads(normalized)
        return settings if isinstance(settings, dict) else {}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return {}


_DOTENV_LINE = re.compile(
    r"(?:^)\s*(?:export\s+)?([\w.-]+)(?:\s*=\s*?|:\s+?)"
    r"(\s*'(?:\\'|[^'])*'|\s*\"(?:\\\"|[^\"])*\"|\s*`(?:\\`|[^`])*`|[^#\r\n]+)?"
    r"\s*(?:#.*)?(?:$)",
    re.MULTILINE,
)


def _parse_dotenv(text: str) -> dict[str, str]:
    """Parse home fallback files with the grammar used by dotenv.parse."""
    parsed: dict[str, str] = {}
    normalized = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    for match in _DOTENV_LINE.finditer(normalized):
        key = match.group(1)
        value = (match.group(2) or "").strip()
        quote = value[:1]
        if len(value) >= 2 and quote in "'\"`" and value[-1] == quote:
            value = value[1:-1]
        if quote == '"':
            value = value.replace(r"\n", "\n").replace(r"\r", "\r")
        parsed[key] = value
    return parsed


def _home_env_fallback(qwen_home: Path) -> dict[str, str]:
    """Match Qwen's global-home then legacy-home .env fallback order."""
    candidates = [qwen_home / ".env"]
    if not os.environ.get("QWEN_HOME"):
        candidates.append(qwen_home.parent / ".env")
    fallback: dict[str, str] = {}
    for candidate in candidates:
        try:
            parsed = _parse_dotenv(candidate.read_text(encoding="utf-8"))
        except OSError:
            continue
        for key, value in parsed.items():
            if key not in os.environ:
                fallback.setdefault(key, value)
    return fallback


def _resolve_qwen_env(value: str, fallback: Optional[dict[str, str]] = None) -> str:
    """Match Qwen's $VAR/${VAR} substitution with home-.env fallbacks."""
    custom = fallback or {}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1) or match.group(2)
        if name in custom:
            return custom[name]
        return os.environ.get(name, match.group(0))

    return re.sub(r"\$(?:(\w+)|\{([^}]+)\})", replace, value)


def _runtime_output_setting(settings: dict, fallback: Optional[dict[str, str]] = None) -> Optional[str]:
    advanced = settings.get("advanced")
    if isinstance(advanced, dict) and "runtimeOutputDir" in advanced:
        value = advanced.get("runtimeOutputDir")
        return _resolve_qwen_env(value, fallback) if isinstance(value, str) else None
    return None


def _folder_trust_enabled(system: dict, user: dict) -> bool:
    """Match Qwen's versioned fast-path V1-to-V2 folderTrust migration."""
    value: object = False
    for settings in (system, user):
        nested_found = False
        security = settings.get("security")
        if isinstance(security, dict):
            folder_trust = security.get("folderTrust")
            if isinstance(folder_trust, dict) and isinstance(folder_trust.get("enabled"), bool):
                value = folder_trust["enabled"]
                nested_found = True

        version = settings.get("$version")
        is_v2 = not isinstance(version, bool) and isinstance(version, (int, float)) and version >= 2
        if not nested_found and not is_v2 and isinstance(settings.get("folderTrust"), bool):
            value = settings["folderTrust"]
    return value is True


def _is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve().relative_to(root.expanduser().resolve())
        return True
    except (OSError, RuntimeError, ValueError):
        return False


def _workspace_is_trusted(cwd: str, system: dict, user: dict, qwen_home: Path) -> bool:
    if not _folder_trust_enabled(system, user):
        return True
    trust_path = Path(
        os.environ.get("QWEN_CODE_TRUSTED_FOLDERS_PATH", str(qwen_home / "trustedFolders.json"))
    ).expanduser()
    rules = _read_qwen_settings(trust_path)
    workspace = Path(cwd)
    # Qwen checks positive rules before exact DO_NOT_TRUST rules.
    for rule_path, level in rules.items():
        if not isinstance(rule_path, str):
            continue
        root = Path(rule_path)
        if level == "TRUST_FOLDER" and _is_within(workspace, root):
            return True
        if level == "TRUST_PARENT" and _is_within(workspace, root.parent):
            return True
    for rule_path, level in rules.items():
        if level == "DO_NOT_TRUST":
            try:
                if Path(rule_path).expanduser().resolve() == workspace.expanduser().resolve():
                    return False
            except (OSError, RuntimeError, ValueError):
                continue
    # Qwen's caller treats unknown trust as trusted (`isTrusted ?? true`).
    return True


def _expand_qwen_runtime_tilde(value: str) -> str:
    """Match Qwen 0.21.3: expand only bare ~/ and ~\\ prefixes."""
    if value == "~":
        return str(Path.home())
    if value.startswith(("~/", "~\\")):
        return str(Path.home() / value[2:])
    return value


def _runtime_base_dir(cwd: str) -> Path:
    configured = os.environ.get("QWEN_RUNTIME_DIR")
    qwen_home_value = os.environ.get("QWEN_HOME")
    qwen_home = Path(qwen_home_value).expanduser() if qwen_home_value else PROJECTS_DIR.parent.expanduser()
    if not configured:
        system_path = Path(os.environ.get("QWEN_CODE_SYSTEM_SETTINGS_PATH", "/etc/qwen-code/settings.json"))
        defaults_path = Path(
            os.environ.get("QWEN_CODE_SYSTEM_DEFAULTS_PATH", str(system_path.parent / "system-defaults.json"))
        )
        defaults = _read_qwen_settings(defaults_path)
        user = _read_qwen_settings(qwen_home / "settings.json")
        workspace = _read_qwen_settings(Path(cwd) / ".qwen" / "settings.json")
        system = _read_qwen_settings(system_path)

        fallback = _home_env_fallback(qwen_home)
        merged: Optional[str] = None
        for settings in (defaults, user):
            value = _runtime_output_setting(settings, fallback)
            if value is not None:
                merged = value
        workspace_active = Path(cwd).expanduser().resolve() != Path.home().resolve()
        if workspace_active and _workspace_is_trusted(cwd, system, user, qwen_home):
            value = _runtime_output_setting(workspace, fallback)
            if value is not None:
                merged = value
        value = _runtime_output_setting(system, fallback)
        if value is not None:
            merged = value
        configured = merged
    if configured:
        candidate = Path(_expand_qwen_runtime_tilde(configured))
        return (Path.cwd() / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()
    return qwen_home.resolve()


def _project_dir_for_cwd(cwd: str) -> Path:
    normalized = cwd.lower() if os.name == "nt" else cwd
    encoded = re.sub(r"[^a-zA-Z0-9]", "-", normalized)
    return _runtime_base_dir(cwd) / "projects" / encoded


def _qwen_filename_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", value)


def resolve_transcript_path(
    input_json: dict,
    session_id: Optional[str] = None,
    *,
    must_exist: bool = True,
) -> Optional[Path]:
    """Return only the canonical transcript bound to this session and cwd.

    ``must_exist=False`` accepts the canonical path before Qwen has created the
    file. Use it when opening a turn; see ``validate_transcript_path``.
    """
    sid = session_id or input_json.get("session_id") or ""
    cwd = input_json.get("cwd") or ""
    if not isinstance(sid, str) or not isinstance(cwd, str) or not sid or not cwd:
        return None
    runtime_base = _runtime_base_dir(cwd)
    project_dir = runtime_base / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd.lower() if os.name == "nt" else cwd)
    chats_root = project_dir / CHATS_SUBDIR
    expected = validate_transcript_path(
        chats_root / f"{sid}.jsonl", root=chats_root, anchor=runtime_base, must_exist=must_exist
    )
    if expected is None:
        return None

    raw = input_json.get("transcript_path") or ""
    if not raw:
        return expected
    supplied = validate_transcript_path(Path(str(raw)), root=chats_root, anchor=runtime_base, must_exist=must_exist)
    return expected if supplied == expected else None


def resolve_agent_transcript_path(
    input_json: dict,
    session_id: Optional[str] = None,
    agent_id: Optional[str] = None,
) -> Optional[Path]:
    """Return only Qwen's canonical child transcript for this parent/agent."""
    sid = session_id or input_json.get("session_id") or ""
    aid = agent_id or input_json.get("agent_id") or ""
    cwd = input_json.get("cwd") or ""
    raw = input_json.get("agent_transcript_path") or ""
    if not all(isinstance(value, str) and value for value in (sid, aid, cwd, raw)):
        return None
    # Qwen generates UUID-like identifiers. Its on-disk sanitizer maps every
    # other character to "_", so non-canonical hook IDs are collision aliases.
    if re.fullmatch(r"[a-zA-Z0-9_-]+", sid) is None or re.fullmatch(r"[a-zA-Z0-9_-]+", aid) is None:
        return None

    runtime_base = _runtime_base_dir(cwd)
    project_dir = runtime_base / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", cwd.lower() if os.name == "nt" else cwd)
    child_root = project_dir / "subagents" / _qwen_filename_component(sid)
    expected = validate_transcript_path(
        child_root / f"agent-{_qwen_filename_component(aid)}.jsonl",
        root=child_root,
        anchor=runtime_base,
    )
    supplied = validate_transcript_path(Path(raw), root=child_root, anchor=runtime_base)
    return expected if expected is not None and supplied == expected else None


def _is_pid_alive(pid: int) -> bool:
    """Check whether a process with the given PID is still running."""
    if pid <= 0:
        return False
    if platform.system() == "Windows":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _unlink_state_under_lifecycle_lock(state_file: Path, stale: Callable[[], bool]) -> None:
    try:
        with _lifecycle_lock_for_state_file(state_file):
            if state_file.is_file() and stale():
                state_file.unlink()
    except (OSError, TimeoutError):
        # A lifecycle owner or concurrent filesystem change means the candidate
        # is live/unknown. GC is best-effort and must fail closed.
        return


def gc_stale_state_files() -> None:
    """Remove stale state only after locking and revalidating each candidate."""
    if not STATE_DIR.is_dir():
        return
    for state_file in STATE_DIR.glob("state_pid_*.json"):
        key = state_file.stem.replace("state_pid_", "", 1)
        if not key.isdigit():
            continue
        pid = int(key)
        if not _is_pid_alive(pid):

            def process_is_still_dead(candidate_pid: int = pid) -> bool:
                return not _is_pid_alive(candidate_pid)

            _unlink_state_under_lifecycle_lock(state_file, process_is_still_dead)

    cutoff = time.time() - EXPLICIT_STATE_TTL_SECONDS
    for state_file in STATE_DIR.glob("state_session_*.json"):

        def expired(path=state_file) -> bool:
            return path.stat().st_mtime < cutoff

        _unlink_state_under_lifecycle_lock(state_file, expired)
