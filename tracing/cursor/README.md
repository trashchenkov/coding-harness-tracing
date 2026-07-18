# Cursor IDE Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for the Cursor IDE and Cursor CLI. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## Setup
The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, and registers the hooks in `.cursor/hooks.json`.

Pass `--with-skills` to also symlink the `manage-cursor-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage Cursor tracing configuration.

### Plugin install

Cursor 2.5+ users can install via the Cursor marketplace instead of running `install.sh`. The plugin auto-registers every hook event and lazily bootstraps a dedicated Python venv on first hook fire into `~/.arize/harness/cursor-plugin-venv` (kept separate from the `install.sh`-managed `~/.arize/harness/venv` to avoid pip file-ownership conflicts).

```text
/add-plugin Arize-ai/coding-harness-tracing
```

(Or point at your own team / private marketplace that mirrors this repo.)

**Credentials.** The plugin skips the interactive wizard, so configure the backend one of two ways:

- **Recommended:** run the bundled `manage-cursor-tracing` skill once from any agent session — it writes `~/.arize/harness/config.json` for you.
- **Or** export `ARIZE_API_KEY` + `ARIZE_SPACE_ID` (Arize AX) or `PHOENIX_ENDPOINT` (Phoenix) in the environment Cursor launches from. macOS GUI caveat: a GUI-launched Cursor may not inherit exports from your shell profile, so the config.json route is more reliable than env vars on macOS.

If no backend is configured, hooks fail open (no-op) — they never block Cursor.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- cursor
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall cursor
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat cursor
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall cursor
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh cursor
```

Uninstall:

```bash
./install.sh uninstall cursor
```

**Windows (PowerShell)**

Install:

```powershell
install.bat cursor
```

Uninstall:

```powershell
install.bat uninstall cursor
```

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `cursor` |
| Project name | `cursor` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook config file | `.cursor/hooks.json` |
| Hook events registered | `beforeSubmitPrompt`, `afterAgentResponse`, `afterAgentThought`, `beforeShellExecution`, `afterShellExecution`, `beforeMCPExecution`, `afterMCPExecution`, `beforeReadFile`, `afterFileEdit`, `stop`, `beforeTabFileRead`, `afterTabFileEdit`, `sessionStart`, `sessionEnd`, `preToolUse`, `postToolUse`, `postToolUseFailure`, `subagentStart`, `subagentStop`, `preCompact`, `workspaceOpen` |
| Host dispatch | Cursor documents Agent, Tab, and app-lifecycle categories. The exact events observed depend on the host, build, mode, and action; do not infer IDE versus CLI from payload key casing. |
| State directory | `~/.arize/harness/state/cursor/` |
| Log file | `~/.arize/harness/logs/cursor.log` |

## Privacy controls

Content capture is on by default. Redact categories independently before
exporting spans:

```bash
export ARIZE_LOG_PROMPTS=false       # user prompts
export ARIZE_LOG_MODEL_OUTPUTS=false # agent responses and thoughts
export ARIZE_LOG_TOOL_CONTENT=false  # tool inputs and outputs
export ARIZE_LOG_TOOL_DETAILS=false  # command text and file paths
```

For example, prompts can remain redacted while model outputs are captured, or
vice versa.

## Verifying tracing

Use Cursor (IDE or `agent` CLI) as normal. The hooks fire on agent activity within the workspace that contains `.cursor/hooks.json`.

- Errors land in `~/.arize/harness/logs/cursor.log` always; set `export ARIZE_VERBOSE=true` before launching Cursor to also see routine hook activity.
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- Verify host invocation separately for the exact Cursor build and surface you use. Handler replay proves parsing and transport, not that a particular IDE or CLI action dispatches a hook.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, etc.).
