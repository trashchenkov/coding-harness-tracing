"""Constants for the Qwen Code tracing harness installer.

Qwen Code's hook contract mirrors Claude Code's, not Gemini CLI's, despite the
Gemini CLI lineage: event names, matcher semantics and payload field names line
up with `tracing/claude_code`, while `tracing/gemini` uses Before*/After* events
that Qwen Code does not emit at all.

Verified against qwen 0.21.3.
"""

from __future__ import annotations

from pathlib import Path

HARNESS_NAME = "qwen"

# Qwen Code settings live at ~/.qwen/settings.json (user) or .qwen/settings.json
# (project). We install user-level by default, same as the Gemini harness.
SETTINGS_DIR = Path.home() / ".qwen"
SETTINGS_FILE = SETTINGS_DIR / "settings.json"

# Session transcripts: ~/.qwen/projects/<encoded-cwd>/chats/<uuid>.jsonl
# Claude Code omits the extra "chats" segment.
PROJECTS_DIR = SETTINGS_DIR / "projects"
CHATS_SUBDIR = "chats"

# The friendly hook name written into the inner hook block of settings.json.
# Used by both install() (to write) and uninstall() (to identify our entries).
HOOK_NAME = "arize-tracing"

# Map of Qwen Code hook event name -> CLI entry-point script name.
# Registered in tracing/qwen/pyproject.toml [project.scripts].
#
# Events absent before 0.21.x (TodoCreated, TodoCompleted, MessageDisplay,
# PermissionDenied) are registered unconditionally: Qwen Code silently ignores
# hook entries for events it does not know, so older CLIs simply never fire
# them. Verified on 0.15.11, which knows only 14 of these.
EVENTS: dict[str, str] = {
    "SessionStart": "arize-hook-qwen-session-start",
    "SessionEnd": "arize-hook-qwen-session-end",
    "UserPromptSubmit": "arize-hook-qwen-user-prompt-submit",
    "PreToolUse": "arize-hook-qwen-pre-tool-use",
    "PostToolUse": "arize-hook-qwen-post-tool-use",
    "PostToolUseFailure": "arize-hook-qwen-post-tool-use-failure",
    "Stop": "arize-hook-qwen-stop",
    "StopFailure": "arize-hook-qwen-stop-failure",
    "SubagentStart": "arize-hook-qwen-subagent-start",
    "SubagentStop": "arize-hook-qwen-subagent-stop",
    "PreCompact": "arize-hook-qwen-pre-compact",
    "PostCompact": "arize-hook-qwen-post-compact",
    "Notification": "arize-hook-qwen-notification",
    "PermissionRequest": "arize-hook-qwen-permission-request",
    "PermissionDenied": "arize-hook-qwen-permission-denied",
    "TodoCreated": "arize-hook-qwen-todo-created",
    "TodoCompleted": "arize-hook-qwen-todo-completed",
}

# Default per-hook timeout in milliseconds.
HOOK_TIMEOUT_MS = 30000

# Todo hooks fire twice per item — once per phase. Only the post-write phase
# reports a durable change; the validation phase exists to block the write and
# would otherwise double every todo span.
TODO_PHASE_POSTWRITE = "postWrite"
TODO_PHASE_VALIDATION = "validation"

# Transcript field names that differ from Claude Code's.
TRANSCRIPT_TOOL_RESULT_KEY = "toolCallResult"  # Claude Code: "toolUseResult"
