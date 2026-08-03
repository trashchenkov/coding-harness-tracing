---
name: manage-qwen-tracing
description: Set up and configure Arize tracing for Qwen Code sessions. Use when users want to set up tracing, configure Arize AX or Phoenix for Qwen Code, enable/disable tracing, or troubleshoot tracing issues. Triggers on "set up qwen tracing", "configure Arize for Qwen Code", "configure Phoenix for Qwen Code", "enable qwen tracing", "setup-qwen-tracing", or any request about connecting Qwen Code to Arize or Phoenix for observability.
---

# Setup Qwen Code Tracing

Configure OpenInference tracing for **Qwen Code** sessions to Arize AX (cloud) or Phoenix (self-hosted). Spans are sent directly to the backend from hooks -- no background process or backend-specific dependencies are needed in the user's environment.

## Qwen-specific notes

Verified against **qwen 0.21.3**.

- The traced unit is the **turn**, not the session: Qwen Code does not emit
  `SessionEnd` in one-shot mode, so a session-scoped root span would never close.
- `UserPromptSubmit` can fire for machine continuations. An explicit
  `submitted_prompt` identifies a genuine user turn; older/headless payloads
  fall back to non-empty `prompt` only when no turn is active.
- Failed transcript reads or exports retain the turn state, including during
  `SessionEnd`, so retries preserve trace and span identities. Lifecycle hooks
  share one per-session lock; running subagents keep the turn non-terminal, and
  reusable advisory lock files remain after state cleanup to avoid inode races.
- Installer updates preserve foreign hooks, serialize cooperating writers, and
  abort instead of overwriting if `settings.json` changes after it is read.
- Todo hooks fire twice per item, once per phase; only `postWrite` is recorded.
- Subagents always produce an `AGENT` span. A distinct background child
  transcript is parsed beneath it; a foreground path equal to the parent is not
  reparsed.
- Qwen Code 0.21.3 exposes 22 events. The harness registers the 17 events in
  `tracing/qwen/constants.py`; older CLIs ignore event names they do not know.


## How to Use This Skill

**This skill follows a decision tree workflow.** Start by asking the user where they are in the setup process:

1. **Is the harness already installed?**
   - Resolve settings as `$QWEN_HOME/settings.json` when `QWEN_HOME` is set,
     otherwise `~/.qwen/settings.json`
   - Check that file for the 17 `arize-tracing` hook entries
   - Check `~/.arize/harness/config.json` for the `harnesses.qwen` block
   - If both are present -> Jump to [Validate](#validate) or [Troubleshoot](#troubleshoot)

2. **Do they already have credentials?**
   - Yes -> Jump to [Configure Settings](#configure-settings)
   - No -> Continue to step 3

3. **Which backend do they want to use?**
   - Phoenix (self-hosted) -> Go to [Set Up Phoenix](#set-up-phoenix)
   - Arize AX (cloud) -> Go to [Set Up Arize AX](#set-up-arize-ax)

4. **Are they troubleshooting?**
   - Yes -> Jump to [Troubleshoot](#troubleshoot)

**Important:** Only follow the relevant path for the user's needs. Don't go through all sections.

## Set Up Phoenix

Phoenix is self-hosted. No Python dependencies are needed for tracing -- spans are sent directly via `send_span()` using stdlib `urllib`.

### Install Phoenix

Ask if they already have Phoenix running. If not, walk through:

```bash
# Option A: pip
pip install arize-phoenix && phoenix serve

# Option B: Docker
docker run -p 6006:6006 arizephoenix/phoenix:latest
```

Phoenix UI will be available at `http://localhost:6006`. Confirm it's running:

```bash
curl -sf http://localhost:6006/v1/traces >/dev/null && echo "Phoenix is running" || echo "Phoenix not reachable"
```

Then proceed to [Configure Settings](#configure-settings) with the Phoenix endpoint.

## Set Up Arize AX

Arize AX is available as a SaaS platform or as an on-prem deployment. Users need an account, a space, and an API key.

**First, ask the user: "Are you using the Arize SaaS platform or an on-prem instance?"**

- **SaaS** -> Uses the default endpoint (`otlp.arize.com:443`). Continue below.
- **On-prem** -> The user will need to provide their custom OTLP endpoint (e.g., `otlp.mycompany.arize.com:443`). Ask for it and note it for the [Configure Settings](#configure-settings) step.

### 1. Create an account

If the user doesn't have an Arize account:
- **SaaS**: Sign up at https://app.arize.com/auth/join
- **On-prem**: Contact their administrator for access to the on-prem instance

### 2. Get Space ID and API key

Walk the user through finding their credentials:
1. Log in to their Arize instance (https://app.arize.com for SaaS, or their on-prem URL)
2. Click **Settings** (gear icon) in the left sidebar
3. The **Space ID** is shown on the Space Settings page
4. Go to the **API Keys** tab
5. Click **Create API Key** or copy an existing one

Both `api_key` and `space_id` are required for the shared config.

**No Python dependencies are needed.** Both Phoenix and Arize AX use HTTP/JSON -- no additional Python dependencies are needed.

Then proceed to [Configure Settings](#configure-settings). If the user is on an on-prem instance, remind them to provide their custom endpoint.

## Configure Settings

**Important:** Users must run this setup before tracing will work. The `send_span()` function requires `~/.arize/harness/config.json` to exist for backend credential resolution.

### Ask the user for:

1. **Backend choice**: Phoenix or Arize AX
2. **Credentials** (only if no existing config):
   - Phoenix: endpoint URL (default: `http://localhost:6006`), optional API key
   - Arize AX: API key and Space ID
3. **OTLP Endpoint** (Arize AX only, optional): For hosted Arize instances using a custom endpoint. Defaults to `otlp.arize.com:443`.
4. **Project name** (optional): defaults to `"qwen"`, stored under `harnesses.qwen.project_name`
5. **User ID** (optional): Set `ARIZE_USER_ID` env var to identify spans by user (useful for teams)

### Write the config

The config file at `~/.arize/harness/config.json` is the single source of truth for backend credentials and per-harness settings. Create the directory structure if needed: `mkdir -p ~/.arize/harness/{bin,run,logs,state/qwen}`

**Important: read-merge-write.** If `~/.arize/harness/config.json` already exists, read it first, then merge in the new or updated fields (e.g., add/update the `harnesses.qwen` entry) while preserving existing backend credentials. Only prompt for backend credentials if no existing config is found.

**Phoenix:**
```json
{
  "harnesses": {
    "qwen": {
      "project_name": "qwen",
      "target": "phoenix",
      "endpoint": "<endpoint>",
      "api_key": ""
    }
  }
}
```

**Arize AX:**
```json
{
  "harnesses": {
    "qwen": {
      "project_name": "qwen",
      "target": "arize",
      "endpoint": "otlp.arize.com:443",
      "api_key": "<key>",
      "space_id": "<id>"
    }
  }
}
```

If the user has a custom OTLP endpoint, set it in `harnesses.qwen.endpoint`.

### Activate Qwen Code hooks

Qwen Code reads `$QWEN_HOME/settings.json` when `QWEN_HOME` is set and
`~/.qwen/settings.json` otherwise. Hooks are configured under
`hooks.<EventName>` as arrays of matcher/hook objects. The installer accepts
JSONC on read, preserves unrelated hooks, and atomically rewrites strict JSON.

Install or reinstall via the installer:

```bash
./install.sh qwen
```

To uninstall:

```bash
./install.sh uninstall qwen
```

The installer registers these 17 events with `name: arize-tracing`:
`SessionStart`, `SessionEnd`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`,
`PostToolUseFailure`, `Stop`, `StopFailure`, `SubagentStart`, `SubagentStop`,
`PreCompact`, `PostCompact`, `Notification`, `PermissionRequest`,
`PermissionDenied`, `TodoCreated`, and `TodoCompleted`. Qwen 0.21.3 also exposes
five events for which this harness currently has no handlers: `PostToolBatch`,
`UserPromptExpansion`, `MessageDisplay`, `SessionDelete`, and
`InstructionsLoaded`.

### Validate

1. **Config exists**: verify `~/.arize/harness/config.json` has the correct backend credentials under `harnesses.qwen`.
2. **Phoenix** (if applicable): run `curl -sf <endpoint>/v1/traces >/dev/null`.
3. **Hooks active**: inspect the resolved Qwen settings path and verify all 17 entries named `arize-tracing`.
4. **Quick dry-run test** (optional):
   ```bash
   printf '%s\n' '{"session_id":"dry-run","hook_event_name":"UserPromptSubmit","submitted_prompt":"hello","prompt":"hello"}' | ARIZE_DRY_RUN=true arize-hook-qwen-user-prompt-submit
   ```

### Confirm

Tell the user:
- Config saved to `~/.arize/harness/config.json`
- Qwen Code hooks activated via `~/.qwen/settings.json`
- Spans are sent directly to the backend from hooks -- no background process needed
- After saving, open a new Qwen Code session and traces will appear in their Phoenix UI or Arize AX dashboard under the project name
- Mention `ARIZE_DRY_RUN=true` to test without sending data (set as env var before launching Qwen Code)
- Mention `ARIZE_VERBOSE=true` for debug output
- Errors are always written to `~/.arize/harness/logs/qwen.log`; set `ARIZE_VERBOSE=true` in the shell before launching Qwen Code to also capture routine hook activity
- Toggle tracing on/off via `ARIZE_TRACE_ENABLED` env var (must be exported in the user's shell -- Qwen Code hooks read host env vars)
- Tail the log file at `~/.arize/harness/logs/qwen.log` for real-time debugging

## Hook Events

The harness registers 17 Claude-style Qwen hook events:

| Events | Trace effect |
|---|---|
| `UserPromptSubmit`, `Stop`, `StopFailure` | Open/export one turn; failure marks the root error |
| `PreToolUse`, `PostToolUse`, `PostToolUseFailure` | Track tool timing, output, and errors |
| `SubagentStart`, `SubagentStop` | Build `AGENT` spans and parse distinct child transcripts |
| `SessionStart`, `SessionEnd` | Initialize/clean state and export any open turn on shutdown |
| `TodoCreated`, `TodoCompleted` | Count durable `postWrite` todo transitions |
| `PreCompact`, `PostCompact` | Log context compaction |
| `Notification`, `PermissionRequest`, `PermissionDenied` | Log notification and permission activity |

## Troubleshoot

Common issues and fixes for Qwen Code:

| Problem | Fix |
|---------|-----|
| Traces not appearing | Verify config exists: `cat ~/.arize/harness/config.json`. Check hook log: `tail -20 ~/.arize/harness/logs/qwen.log` |
| Hooks not firing | Resolve `$QWEN_HOME/settings.json` or `~/.qwen/settings.json`; verify the 17 entries named `arize-tracing` |
| Config missing | Run `./install.sh qwen` or create `~/.arize/harness/config.json` manually (include `harnesses.qwen` section) |
| Phoenix unreachable | Verify Phoenix is running: `curl -sf <endpoint>/v1/traces` |
| Want to test without sending | Set `ARIZE_DRY_RUN=true` env var before launching Qwen Code |
| Want verbose logging | Set `ARIZE_VERBOSE=true` env var before launching Qwen Code |
| Wrong project name | Set `harnesses.qwen.project_name` in `~/.arize/harness/config.json` (default: `"qwen"`) |
| Spans missing user attribution | Set `ARIZE_USER_ID` env var before launching Qwen Code |
| Tracing not toggling | Ensure `ARIZE_TRACE_ENABLED` is exported in your shell, not just set |
