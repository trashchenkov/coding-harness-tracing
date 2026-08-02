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

import os
import platform
from pathlib import Path
from typing import Optional

from core.common import StateManager, env, generate_trace_id, get_timestamp_ms, log, redirect_stderr_to_log_file
from core.constants import HARNESSES, STATE_BASE_DIR
from tracing.qwen.constants import CHATS_SUBDIR, PROJECTS_DIR

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["qwen"]
SERVICE_NAME = _HARNESS["service_name"]  # "qwen"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-qwen-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/qwen

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


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
        session_key = sid

    if not session_key:
        env_key = os.environ.get("QWEN_SESSION_KEY", "")
        if env_key:
            session_key = env_key

    if not session_key:
        ppid = os.getppid()
        session_key = str(ppid if ppid > 0 else os.getpid())

    state_file = STATE_DIR / f"state_{session_key}.json"
    lock_path = STATE_DIR / f".lock_{session_key}"

    sm = StateManager(
        state_dir=STATE_DIR,
        state_file=state_file,
        lock_path=lock_path,
    )
    sm.init_state()
    return sm


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
    if not project_name:
        cwd = input_json.get("cwd", "")
        project_name = os.path.basename(cwd) if cwd else os.path.basename(os.getcwd())

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("project_name", project_name)
    state.set("trace_count", "0")
    state.set("tool_count", "0")

    # SessionStart carries the resolved model name; earlier events may not.
    # Recorded when offered so spans can report it without guessing.
    model = input_json.get("model", "")
    if model:
        state.set("model", model)

    user_id = env.get_user_id(SERVICE_NAME)
    if not user_id:
        user_id = input_json.get("user_id", "")
    state.set("user_id", user_id)

    log(f"Session initialized: {session_id}")


def resolve_transcript_path(input_json: dict, session_id: Optional[str] = None) -> Optional[Path]:
    """Return the session transcript path, or None when it cannot be located.

    Qwen Code supplies ``transcript_path`` on every hook event observed, so the
    reconstruction below is a fallback for truncated payloads. Note the extra
    ``chats`` path segment, which Claude Code does not have::

        ~/.qwen/projects/<encoded-cwd>/chats/<session-uuid>.jsonl
    """
    raw = input_json.get("transcript_path") or ""
    if raw:
        path = Path(raw)
        if path.is_file():
            return path

    sid = session_id or input_json.get("session_id") or ""
    cwd = input_json.get("cwd") or ""
    if not sid or not cwd:
        return None

    # Qwen encodes the working directory by replacing path separators with '-'.
    encoded = str(cwd).replace(os.sep, "-")
    if os.altsep:
        encoded = encoded.replace(os.altsep, "-")
    candidate = PROJECTS_DIR / encoded / CHATS_SUBDIR / f"{sid}.jsonl"
    return candidate if candidate.is_file() else None


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


def gc_stale_state_files() -> None:
    """Remove state files for PIDs that are no longer running.

    Only cleans numeric (PID-based) filenames such as ``state_12345.json``;
    UUID-keyed session files are left to the SessionEnd handler.
    """
    if not STATE_DIR.is_dir():
        return
    for f in STATE_DIR.glob("state_*.json"):
        key = f.stem.replace("state_", "", 1)
        if not key.isdigit():
            continue
        if not _is_pid_alive(int(key)):
            try:
                f.unlink()
            except OSError:
                pass
