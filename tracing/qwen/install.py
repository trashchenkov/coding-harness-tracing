#!/usr/bin/env python3
"""Qwen Code tracing harness installer.

Handles install and uninstall for Qwen Code tracing hooks. Qwen Code uses a
single ~/.qwen/settings.json with a hooks dict keyed by event name, in the same
shape Gemini CLI uses — but with Claude-Code-style event names.

Usage (called by the shell router):
    python tracing/qwen/install.py install
    python tracing/qwen/install.py uninstall
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NoReturn, Optional, Tuple

from core.common import FileLock
from core.config import get_value, load_config
from core.setup import (
    dry_run,
    ensure_shared_runtime,
    info,
    merge_harness_entry,
    prompt_backend,
    prompt_content_logging,
    prompt_project_name,
    prompt_user_id,
    remove_harness_entry,
    unlink_skills,
    venv_bin,
    write_config,
    write_logging_config,
)
from tracing.qwen.constants import EVENTS, HARNESS_NAME, HOOK_NAME, HOOK_TIMEOUT_MS

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------


def _settings_file():
    """Return Qwen's active settings path, including ``QWEN_HOME``."""
    import tracing.qwen.constants as _c

    qwen_home = os.environ.get("QWEN_HOME")
    if qwen_home:
        return Path(qwen_home).expanduser() / "settings.json"
    return _c.SETTINGS_FILE


def _settings_dir():
    """Return Qwen's active settings directory, including ``QWEN_HOME``."""
    import tracing.qwen.constants as _c

    qwen_home = os.environ.get("QWEN_HOME")
    if qwen_home:
        return Path(qwen_home).expanduser()
    return _c.SETTINGS_DIR


def _strip_json_comments(text: str) -> str:
    """Remove JavaScript comments while preserving markers inside strings."""
    output: list[str] = []
    index = 0
    in_string = False
    escaped = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            output.append(char)
            index += 1
            continue
        if char == "/" and following == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and following == "*":
            index += 2
            while index + 1 < len(text) and text[index : index + 2] != "*/":
                output.append("\n" if text[index] == "\n" else " ")
                index += 1
            index = min(index + 2, len(text))
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _strip_jsonc_trailing_commas(text: str) -> str:
    """Remove JSONC trailing commas without touching string contents."""
    output: list[str] = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if in_string:
            output.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
            output.append(char)
            continue
        if char == ",":
            following = index + 1
            while following < len(text) and text[following].isspace():
                following += 1
            if following < len(text) and text[following] in "]}":
                continue
        output.append(char)
    return "".join(output)


def _invalid_settings(message: str) -> NoReturn:
    """Abort without modifying a settings file whose JSON shape is unsafe."""
    from core.setup import err as _err

    _err(f"{_settings_file()} has an invalid schema: {message}; aborting")
    raise SystemExit(1)


def _validate_settings(data: object) -> dict:
    """Validate the settings levels that installer merge operations mutate."""
    if not isinstance(data, dict):
        _invalid_settings("root must be a JSON object")
    if "hooks" not in data:
        return data
    hooks = data["hooks"]
    if not isinstance(hooks, dict):
        _invalid_settings("'hooks' must be a JSON object")
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            _invalid_settings(f"hooks.{event} must be a JSON array")
    return data


def _read_settings() -> dict:
    """Read settings.json, returning empty dict on missing or empty files.

    Raises ``SystemExit(1)`` on malformed JSON or permission errors so we never
    silently overwrite a user file.
    """
    path = _settings_file()
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        from core.setup import err as _err

        _err(f"Cannot read {path}: {exc}")
        sys.exit(1)
    if not text.strip():
        info("settings.json is empty, treating as {}")
        return {}
    try:
        normalized = _strip_json_comments(text.lstrip("\ufeff"))
        data = json.loads(_strip_jsonc_trailing_commas(normalized))
    except json.JSONDecodeError as exc:
        from core.setup import err as _err

        _err(f"{path} contains invalid JSON; aborting. Please fix the file and retry.\n  {exc}")
        sys.exit(1)
    return _validate_settings(data)


def _settings_fingerprint(path: Path) -> tuple[int, int, int, str] | None:
    """Return an optimistic identity for conflict detection."""
    try:
        payload = path.read_bytes()
        stat = path.stat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        from core.setup import err as _err

        _err(f"Cannot inspect {path}: {exc}")
        sys.exit(1)
    return (stat.st_ino, stat.st_size, stat.st_mtime_ns, hashlib.sha256(payload).hexdigest())


def _abort_if_settings_changed(path: Path, expected: tuple[int, int, int, str] | None) -> None:
    if _settings_fingerprint(path) != expected:
        from core.setup import err as _err

        _err(f"{path} changed while Qwen hooks were being updated; aborting without overwrite. Please retry.")
        sys.exit(1)


SettingsFingerprint = Optional[Tuple[int, int, int, str]]


class _NoConflictCheck:
    pass


_NO_CONFLICT_CHECK = _NoConflictCheck()


def _write_settings(
    data: dict,
    *,
    expected_fingerprint: SettingsFingerprint | _NoConflictCheck = _NO_CONFLICT_CHECK,
) -> None:
    """Atomically write settings, aborting if an external writer won the race."""
    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(json.dumps(data, indent=2) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.chmod(mode)
        if not isinstance(expected_fingerprint, _NoConflictCheck):
            _abort_if_settings_changed(path, expected_fingerprint)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()


def _remove_named_hook(block: object) -> object | None:
    """Remove our inner hook while preserving neighbouring hooks."""
    if not isinstance(block, dict):
        return block
    inner_hooks = block.get("hooks")
    if not isinstance(inner_hooks, list):
        return block
    remaining = [hook for hook in inner_hooks if not (isinstance(hook, dict) and hook.get("name") == HOOK_NAME)]
    if not remaining:
        return None
    cleaned = dict(block)
    cleaned["hooks"] = remaining
    return cleaned


def _without_our_hooks(event_list: list) -> list:
    """Remove only Arize commands from an event's matcher blocks."""
    cleaned = (_remove_named_hook(block) for block in event_list)
    return [block for block in cleaned if block is not None]


# ---------------------------------------------------------------------------
# Install / uninstall hooks in settings.json
# ---------------------------------------------------------------------------


def _quote_command(command: object, *, platform_name: str | None = None) -> str:
    """Quote one executable path for the command parser used by the host OS."""
    value = str(command)
    if (platform_name or os.name) == "nt":
        return subprocess.list2cmdline([value])
    return shlex.quote(value)


def _settings_update_lock(path: Path) -> FileLock:
    return FileLock(path.with_name(f".{path.name}.arize.lock"), timeout=10.0, break_on_timeout=False)


def _install_hooks() -> None:
    """Merge the 17 supported hook entries into Qwen's settings file."""
    if dry_run():
        info(f"would write Qwen Code hooks to {_settings_file()}")
        return

    path = _settings_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _settings_update_lock(path):
        expected = _settings_fingerprint(path)
        data = _read_settings()
        hooks_map: dict = data.setdefault("hooks", {})

        for event, entry_point in EVENTS.items():
            cmd = _quote_command(venv_bin(entry_point))
            event_list: list = hooks_map.setdefault(event, [])

            # Other commands may legitimately share this matcher block.
            event_list[:] = _without_our_hooks(event_list)
            event_list.append(
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "name": HOOK_NAME,
                            "command": cmd,
                            "timeout": HOOK_TIMEOUT_MS,
                        }
                    ],
                }
            )

        _write_settings(data, expected_fingerprint=expected)


def _uninstall_hooks() -> None:
    """Remove our hook entries from ~/.qwen/settings.json."""
    path = _settings_file()
    if not path.is_file():
        return

    if dry_run():
        info(f"would remove Qwen Code hooks from {path}")
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    with _settings_update_lock(path):
        if not path.is_file():
            return
        expected = _settings_fingerprint(path)
        data = _read_settings()
        hooks_map = data.get("hooks", {})

        for event in EVENTS:
            event_list = hooks_map.get(event, [])
            filtered = _without_our_hooks(event_list)
            if filtered:
                hooks_map[event] = filtered
            else:
                hooks_map.pop(event, None)

        if not hooks_map:
            data.pop("hooks", None)

        if not data:
            _abort_if_settings_changed(path, expected)
            path.unlink()
        else:
            _write_settings(data, expected_fingerprint=expected)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def install() -> None:
    """Install Qwen Code tracing hooks and register in config.json."""
    ensure_shared_runtime()

    config = load_config()
    existing_entry = get_value(config, f"harnesses.{HARNESS_NAME}")

    if not existing_entry or not isinstance(existing_entry, dict) or "target" not in existing_entry:
        existing_harnesses = config.get("harnesses") if config else None
        target, credentials = prompt_backend(existing_harnesses)
        project_name = prompt_project_name(HARNESS_NAME)
        user_id = prompt_user_id()
        if not dry_run():
            write_config(target, credentials, HARNESS_NAME, project_name, user_id=user_id)
        else:
            info("would write config.json with backend credentials")
    else:
        project_name = prompt_project_name(existing_entry.get("project_name") or HARNESS_NAME)
        merge_harness_entry(HARNESS_NAME, project_name)

    # Logging settings are global. Prompt only if no `logging:` block exists yet.
    if (config.get("logging") if config else None) is None:
        logging_block = prompt_content_logging()
        write_logging_config(logging_block)
    else:
        info("Using existing logging settings from config.json")

    _install_hooks()

    info("Qwen Code tracing installed")


def uninstall() -> None:
    """Remove Qwen Code tracing hooks and deregister from config.json."""
    _uninstall_hooks()

    remove_harness_entry(HARNESS_NAME)
    unlink_skills(HARNESS_NAME)
    info("Qwen Code tracing uninstalled")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Dispatch install / uninstall from the command line."""
    if len(sys.argv) < 2 or sys.argv[1] not in ("install", "uninstall"):
        print(f"usage: {sys.argv[0]} {{install|uninstall}}", file=sys.stderr)
        sys.exit(1)

    action = sys.argv[1]

    if action == "install":
        install()
    else:
        uninstall()


if __name__ == "__main__":
    main()
