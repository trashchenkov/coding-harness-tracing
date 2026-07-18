# omp Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for [Oh My Pi (omp)](https://github.com/can1357/oh-my-pi) terminal coding sessions. Spans are exported to [Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

## What gets traced

One trace per **agent run** — one user prompt → the agent's internal turn/tool-use loop → its final answer. A single agent run usually contains several model calls (omp re-prompts the model after each round of tool calls), so one trace covers all of them. Each trace is a tree:

| Span | Kind | Notes |
|------|------|-------|
| `Turn` | CHAIN | Root span. `input.value` is the user prompt (from `before_agent_start`); `output.value` is the final assistant message's text. One per agent run. |
| `LLM: <model>` | LLM | Child of `Turn`. One per `turn_end` (one per model call in the loop). Carries `llm.model_name`, `llm.provider`, prompt/completion/reasoning token counts, cache read/write tokens, and `llm.cost`. |
| `<tool>` | TOOL | Child of the `LLM` span that requested it. One per `ToolResultMessage` in a `turn_end`, paired with its originating `ToolCall` by id. Records `tool.name`, input args + output subject to the content flags below, and tool-specific attributes. Errors are recorded with span status. |

Token usage **is** captured — omp surfaces cumulative `usage` (input/output/reasoning tokens, cache read/write, and cost) inline on each assistant message, unlike vendors that withhold it from local surfaces.

Timestamps come from omp's own millisecond clocks where present (`AssistantMessage.timestamp` / `duration`, `turn_start.timestamp`, `ToolResultMessage.timestamp`), falling back to wall-clock time on the tracing process when absent.

## Architecture

OMP loads its extensions **in-process** inside its Bun runtime ([hook docs](https://omp.sh/docs/hooks)). Unlike snapshot-based integrations, OMP exposes rich, normally once-fired lifecycle events that already carry final structured data. The integration is split into two pieces:

1. **TypeScript hook shim** (`~/.omp/extensions/arize-tracing.ts`). A dumb bridge. On a small whitelist of lifecycle events — `before_agent_start` (carries the prompt), `turn_end` (carries the completed `AssistantMessage` with inline token usage + model, plus that turn's `toolResults`), `agent_end` (run finished), and `session_shutdown` — it spawns `arize-hook-omp`, pipes the event payload to stdin, and awaits dispatcher completion for at most 1.5 seconds. This preserves lifecycle state ordering while staying below OMP's 2-second shutdown hook deadline.
2. **Python event handler** (`arize-hook-omp`). A small state machine keyed by session id. It dispatches on `payload["type"]`, accumulates per-session state, and emits `Turn`/`LLM`/`TOOL` spans on receipt. It pairs each `ToolCall` with its `ToolResultMessage` by id to build TOOL spans with both input args and output.

The handler derives stable LLM and TOOL span IDs from the current root span, validated `turnIndex`, and a hash of the tool call id. TOOL replay identity is turn-scoped, so providers may reuse a call id in a later turn without losing a span. Persisted replay markers suppress duplicate exports when an event is retried. A separate per-session dispatch lock serializes the full check → emit → mark transaction across dispatcher processes while leaving different sessions independent. Session IDs that are unsafe or oversized for filenames are represented by stable SHA-256 filesystem tokens. Lock timeout fails soft without deleting another process's live lock.

`agent_end` carries omp's authoritative `willContinue` flag. When it is true, the handler keeps the current trace open for the next internal loop; only a terminal `agent_end` or `session_shutdown` closes the root span and clears active trace state.

## Content controls

Structural telemetry remains available when content logging is disabled:

| Environment variable | When `false` |
|----------------------|--------------|
| `ARIZE_LOG_PROMPTS` | Redacts user/assistant text while retaining span topology, model and usage metadata. |
| `ARIZE_LOG_TOOL_DETAILS` | Omits tool-specific detail attributes such as file paths and command fields. |
| `ARIZE_LOG_TOOL_CONTENT` | Redacts tool arguments and results while retaining tool name, call id, timing and status. |

All three default to `true`; set them explicitly for privacy-sensitive environments.

## Setup

The installer prompts for your backend (Phoenix or Arize AX) and project name, writes credentials to `~/.arize/harness/config.json`, copies the hook shim into `~/.omp/extensions/arize-tracing.ts`, **and** registers the shim's absolute path in the `extensions` array of `~/.omp/agent/settings.json`. omp does **not** auto-discover an extensions directory ([extension loading docs](https://omp.sh/docs/hooks)) — explicit registration is required, and the installer handles it. Spans are sent directly to the backend from the handler — no separate buffer/collector service is required.

Pass `--with-skills` to also symlink the `manage-omp-tracing` skill into the current directory's `.agents/skills/` so coding agents in this workspace can help manage omp tracing configuration.

### Remote setup

#### macOS / Linux

Install:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- omp
```

Uninstall:

```bash
curl -sSL https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.sh | bash -s -- uninstall omp
```

#### Windows (PowerShell)

Install:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat omp
```

Uninstall:

```powershell
iwr -useb https://raw.githubusercontent.com/Arize-ai/coding-harness-tracing/main/install.bat -OutFile $env:TEMP\install.bat
& $env:TEMP\install.bat uninstall omp
```

### Local setup

```bash
git clone https://github.com/Arize-ai/coding-harness-tracing.git
cd coding-harness-tracing
```

**macOS / Linux**

Install:

```bash
./install.sh omp
```

Uninstall:

```bash
./install.sh uninstall omp
```

**Windows (PowerShell)**

Install:

```powershell
install.bat omp
```

Uninstall:

```powershell
install.bat uninstall omp
```

Uninstall removes the shim's path from the `extensions` array in `~/.omp/agent/settings.json`, deletes the hook file at `~/.omp/extensions/arize-tracing.ts` (only if it carries the Arize header marker — your own extensions are left alone), and removes the `harnesses.omp` block from `~/.arize/harness/config.json`.

## Default Settings

| Setting | Default |
|---------|---------|
| Harness key | `omp` |
| Project name | `omp` |
| Phoenix endpoint | `http://localhost:6006` |
| Arize AX endpoint | `otlp.arize.com:443` |
| Hook file | `~/.omp/extensions/arize-tracing.ts` |
| Registration | absolute path in `extensions` array of `~/.omp/agent/settings.json` |
| Lifecycle events forwarded | `before_agent_start`, `turn_end`, `agent_end`, `session_shutdown` |
| Span tree | `Turn` (CHAIN) → `LLM` → requesting `TOOL`; additional LLM cycles remain children of `Turn` |
| Trace granularity | one trace per agent run |
| State directory | `~/.arize/harness/state/omp/` |
| Log file | `~/.arize/harness/logs/omp.log` |

## Verifying tracing

Run any omp session as you normally would. omp loads the registered extension on startup; the shim forwards lifecycle events to the Python handler.

- Errors and handler stderr land in `~/.arize/harness/logs/omp.log` always (the adapter redirects Python stderr there via `ARIZE_LOG_FILE`); set `export ARIZE_VERBOSE=true` before launching omp to also see routine handler activity (event dispatch, span emits, state transitions).
- Confirm spans appear in your configured project in Arize AX or Phoenix.
- Set `ARIZE_TRACE_DEBUG=true` to dump the raw event payloads under `~/.arize/harness/state/debug/` (files are named `omp_before_agent_start_<ts>.json`, `omp_turn_end_<ts>.json`, `omp_agent_end_<ts>.json`, `omp_session_shutdown_<ts>.json`) for inspection.

See the [main README's Environment variables section](../../README.md#environment-variables) for the full list of runtime overrides (`ARIZE_TRACE_ENABLED`, `ARIZE_DRY_RUN`, `ARIZE_USER_ID`, `ARIZE_PROJECT_NAME`, `ARIZE_VERBOSE`, `ARIZE_TRACE_DEBUG`, etc.).
