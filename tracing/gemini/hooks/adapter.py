#!/usr/bin/env python3
"""Gemini adapter — single-mode (CLI-only) session resolution, initialization, and GC.

Gemini CLI provides GEMINI_SESSION_ID as an env var on every hook invocation,
but falls back to PID-based session keys (grandparent PID) to ensure
consistent state across all hooks in a single CLI run.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import time

from core.common import (
    FileLock,
    StateManager,
    env,
    generate_trace_id,
    get_timestamp_ms,
    log,
    redirect_stderr_to_log_file,
)
from core.constants import HARNESSES, STATE_BASE_DIR

# --- Module-level constants derived from HARNESSES ---
_HARNESS = HARNESSES["gemini"]
SERVICE_NAME = _HARNESS["service_name"]  # "gemini"
SCOPE_NAME = _HARNESS["scope_name"]  # "arize-gemini-plugin"
STATE_DIR = STATE_BASE_DIR / _HARNESS["state_subdir"]  # ~/.arize/harness/state/gemini
_SAFE_SESSION_KEY = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

# Route hook stderr to a per-harness log file unless the user already set one.
os.environ.setdefault("ARIZE_LOG_FILE", str(_HARNESS["default_log_file"]))
redirect_stderr_to_log_file()


def _get_grandparent_pid() -> str:
    """Get the grandparent PID for session key derivation.

    Gemini CLI spawns: gemini(grandparent) -> node(parent) -> hook(this process).
    On Unix: try reading /proc or using ps command.
    Falls back to parent PID if grandparent can't be determined.
    """
    ppid = os.getppid()
    if ppid <= 0:
        return str(os.getpid())

    # Try /proc (Linux)
    try:
        stat_path = f"{ppid}/stat"
        if os.path.exists(f"/proc/{stat_path}"):
            with open(f"/proc/{stat_path}") as f:
                raw = f.read()
            close_paren = raw.rfind(")")
            rest = raw[close_paren + 2 :].split()
            gpid = rest[1]
            if gpid.isdigit() and int(gpid) > 0:
                return gpid
    except (OSError, IndexError, ValueError):
        pass

    # Try ps command (macOS / other Unix)
    try:
        result = subprocess.check_output(
            ["ps", "-o", "ppid=", "-p", str(ppid)],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        gpid = result.decode().strip()
        if gpid.isdigit() and int(gpid) > 0:
            return gpid
    except (subprocess.SubprocessError, OSError, ValueError):
        pass

    return str(ppid)


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
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
    else:
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False


def check_requirements() -> bool:
    """Return True if env.trace_enabled is True. Create STATE_DIR if so."""
    if not env.trace_enabled:
        return False
    STATE_DIR.mkdir(parents=True, mode=0o700, exist_ok=True)
    if os.name != "nt":
        os.chmod(STATE_DIR, 0o700)
    return True


def _raw_session_key(input_json: dict) -> tuple[str, bool]:
    """Return the authoritative session key and whether it is a PID fallback."""
    value = os.environ.get("GEMINI_SESSION_ID", "")
    if not value:
        value = input_json.get("sessionId") or input_json.get("session_id", "")
    if isinstance(value, str) and value:
        return value, False
    if platform.system() == "Windows":
        return str(os.getppid()), True
    return _get_grandparent_pid(), True


def session_file_key(input_json: dict) -> str:
    """Return a stable, single-component filesystem key for a Gemini session."""
    session_id, is_fallback = _raw_session_key(input_json)
    if is_fallback:
        return f"f_{session_id}"
    if _SAFE_SESSION_KEY.fullmatch(session_id):
        return f"s_{session_id}"
    digest = hashlib.sha256(session_id.encode("utf-8", errors="surrogatepass")).hexdigest()
    return f"h_{digest}"


def dispatch_lock_path(input_json: dict):
    """Return the session-wide event serialization lock path."""
    return STATE_DIR / f".dispatch_{session_file_key(input_json)}"


def _migrate_legacy_state(input_json: dict, new_state_file) -> None:
    """Atomically retain an in-flight state file from the pre-namespace layout.

    Only explicit safe IDs had a predictable legacy filename. The embedded
    session_id must match too, preventing an explicit numeric ID from taking a
    PID-fallback file that happened to use the same legacy basename.
    """
    raw_key, is_fallback = _raw_session_key(input_json)
    if is_fallback or not _SAFE_SESSION_KEY.fullmatch(raw_key) or new_state_file.exists():
        return
    legacy_state = STATE_DIR / f"state_{raw_key}.json"
    if not legacy_state.is_file():
        return
    legacy_lock = STATE_DIR / f".lock_{raw_key}"
    try:
        with FileLock(legacy_lock, timeout=3.0, break_on_timeout=False):
            if new_state_file.exists() or not legacy_state.is_file():
                return
            data = json.loads(legacy_state.read_text())
            if not isinstance(data, dict) or data.get("session_id") != raw_key:
                return
            os.replace(legacy_state, new_state_file)
    except (OSError, TimeoutError, json.JSONDecodeError):
        # Upgrade compatibility is best-effort; normal initialization below
        # remains fail-soft if a legacy writer is still active or data is bad.
        return


def resolve_session(input_json: dict) -> StateManager:
    """Build a StateManager keyed off GEMINI_SESSION_ID env (preferred),
    else input_json sessionId (standard) or session_id (legacy),
    else grandparent PID (to keep state consistent across hooks).
    """
    key = session_file_key(input_json)

    state_file = STATE_DIR / f"state_{key}.json"
    lock_path = STATE_DIR / f".lock_{key}"
    _migrate_legacy_state(input_json, state_file)

    sm = StateManager(
        state_dir=STATE_DIR,
        state_file=state_file,
        lock_path=lock_path,
    )
    sm.init_state()
    return sm


def ensure_session_initialized(state: StateManager, input_json: dict) -> None:
    """Idempotent session initialization."""
    existing = state.get("session_id")
    if existing is not None:
        return

    # Prefer the Gemini-provided session identifier so Arize spans correlate
    # back to the same session in Gemini. Fall back to a generated trace ID
    # (never the PID, which can collide across runs).
    session_id = (
        os.environ.get("GEMINI_SESSION_ID")
        or input_json.get("sessionId")
        or input_json.get("session_id")
        or generate_trace_id()
    )

    # project_name
    project_name = env.project_name_for(SERVICE_NAME)
    if not project_name:
        cwd = input_json.get("projectDir") or input_json.get("cwd", "")
        project_name = os.path.basename(cwd) if cwd else os.path.basename(os.getcwd())

    state.set("session_id", session_id)
    state.set("session_start_time", str(get_timestamp_ms()))
    state.set("project_name", project_name)
    state.set("trace_count", "0")
    state.set("tool_count", "0")

    user_id = env.get_user_id(SERVICE_NAME)
    state.set("user_id", user_id)

    log(f"Session initialized: {session_id}")


def gc_stale_state_files() -> None:
    """Remove stale state only while holding that session's dispatch and state locks.

    Lock artifacts intentionally persist: unlinking a lock inode can split
    waiters across old and newly-created inodes, defeating mutual exclusion.
    """
    if not STATE_DIR.is_dir():
        return
    cutoff = time.time() - 86400

    def _is_stale(path, key: str) -> bool:
        pid_key = key[2:] if key.startswith("f_") else key
        is_pid_key = key.isdigit() or (key.startswith("f_") and pid_key.isdigit())
        if is_pid_key:
            return not _is_pid_alive(int(pid_key))
        return path.stat().st_mtime < cutoff

    for f in STATE_DIR.glob("state_*.json"):
        key = f.stem.replace("state_", "", 1)
        try:
            if not _is_stale(f, key):
                continue
            # Never break or unlink a live lock. Re-check after acquiring both
            # locks because another hook may have refreshed the state meanwhile.
            with FileLock(STATE_DIR / f".dispatch_{key}", timeout=0.05, break_on_timeout=False):
                state_lock = STATE_DIR / f".lock_{key}"
                # Directory locks are the no-flock fallback. Once dispatch is
                # exclusively held, no current handler can own the state lock;
                # a leftover directory is therefore an orphan from a crash.
                if state_lock.is_dir():
                    state_lock.rmdir()
                with FileLock(state_lock, timeout=0.05, break_on_timeout=False):
                    if f.exists() and _is_stale(f, key):
                        f.unlink(missing_ok=True)
        except (OSError, TimeoutError):
            # GC is best-effort; an active or temporarily contended session wins.
            continue
