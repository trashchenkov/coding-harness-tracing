"""Constants for the Cursor tracing harness."""

from pathlib import Path

HARNESS_NAME = "cursor"
DISPLAY_NAME = "Cursor"
HARNESS_HOME = ".cursor"  # ~/.cursor — presence check for soft install detection
HARNESS_BIN = "cursor"  # binary name for shutil.which() fallback

HOOKS_FILE = Path.home() / ".cursor" / "hooks.json"
HOOK_BIN_NAME = "arize-hook-cursor"

# Cursor 2.5+ hook inventory, all routed to a single entry point. The
# documented stdin discriminator is ``hook_event_name``; the handler keeps
# older aliases as fail-open compatibility only.
HOOK_EVENTS = (
    "beforeSubmitPrompt",
    "afterAgentResponse",
    "afterAgentThought",
    "beforeShellExecution",
    "afterShellExecution",
    "beforeMCPExecution",
    "afterMCPExecution",
    "beforeReadFile",
    "afterFileEdit",
    "stop",
    "beforeTabFileRead",
    "afterTabFileEdit",
    "sessionStart",
    "sessionEnd",
    "preToolUse",
    "postToolUse",
    "postToolUseFailure",
    "subagentStart",
    "subagentStop",
    "preCompact",
    "workspaceOpen",
)
