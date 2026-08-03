# Qwen Code Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for
[Qwen Code](https://github.com/QwenLM/qwen-code) sessions. Spans are exported to
[Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

Developed and verified against **qwen 0.21.3**.

## Setup

The installer prompts for your backend (Phoenix or Arize AX) and project name,
writes credentials to `~/.arize/harness/config.json`, and registers the hooks in
`$QWEN_HOME/settings.json` when `QWEN_HOME` is set, otherwise
`~/.qwen/settings.json`. Existing JSONC comments are accepted on read; writes
preserve unrelated hook entries, serialize installer operations with a file
lock, and replace the file atomically. If another process changes the settings
after they were read, installation aborts without overwriting that update.

```bash
python tracing/qwen/install.py install
python tracing/qwen/install.py uninstall
```

## What you get

One trace per user turn:

```text
Turn (CHAIN)
├── LLM call 1 (LLM)          model, prompt/completion/total tokens
│   ├── run_shell_command (TOOL)
│   └── agent (TOOL)
│       └── Subagent (AGENT)  id, type, final answer
│           └── child LLM/TOOL spans when Qwen supplies a separate transcript
└── LLM call 2 (LLM)
```

The turn — not the session — is the traced unit. Qwen Code does not emit
`SessionEnd` in one-shot mode (`qwen "prompt"`), so a session-scoped root span
would never close there. `UserPromptSubmit` opens a turn and `Stop` exports a
snapshot. A successful Stop remains reopenable because another blocking Stop
hook can feed a continuation back to Qwen; a later Stop updates the same
trace/span identities. An identical duplicate Stop is ignored. A genuine next
user prompt or `SessionEnd` is the definitive boundary that clears an already
exported turn. Failed transcript reads or exports retain durable turn state so a
retry also reuses the same identities.

Turn opening, Stop/StopFailure, subagent updates, and SessionEnd are serialized
by one per-session lifecycle lock. A running subagent keeps the turn retryable
until its terminal hook arrives. Definitive cleanup removes turn state but
intentionally retains the reusable advisory lock file: unlinking an active lock
could split concurrent callers across different inodes.

## How this differs from the Gemini harness

Qwen Code began as a fork of Gemini CLI, but its **hook contract follows Claude
Code**. `tracing/gemini` registers `BeforeModel` / `AfterModel` / `BeforeTool` /
`AfterTool`; Qwen Code emits none of those. Its event names and payload fields
line up with `tracing/claude_code` instead, down to `tool_use_id`,
`permission_mode` and `stop_hook_active`.

Transcripts, however, are a hybrid — a Claude-shaped envelope around Google
GenAI message bodies:

| | Claude Code | Qwen Code |
| --- | --- | --- |
| transcript path | `projects/<cwd>/<uuid>.jsonl` | `projects/<cwd>/chats/<uuid>.jsonl` |
| message body | typed content blocks | `parts` with `functionCall` / `functionResponse` |
| assistant role | `assistant` | `model` |
| token usage | `message.usage` | `usageMetadata`, Gemini-named |
| tool result | `toolUseResult` | `toolCallResult` |

## Behaviours worth knowing

**Todo hooks fire twice per item.** `TodoCreated` and `TodoCompleted` run once
in the `validation` phase, which exists to block the write, and once in
`postWrite`. Only `postWrite` is durable — the harness ignores the other, since
counting both would double every todo.

**Continuation prompts are not new turns.** Qwen Code can re-fire
`UserPromptSubmit` after tool results to continue the same turn. A hook payload
with an explicit `submitted_prompt` opens a turn; machine continuations without
that provenance do not. Older/headless payloads fall back to a non-empty
`prompt` only when no turn is active.

**Subagent transcript paths depend on execution mode.** Foreground
`SubagentStop` may point at the parent transcript; reparsing that path would
duplicate the parent spans. Background execution can provide a separate child
JSONL. The harness compares canonical paths, always reports the `AGENT`
invocation/timing/final answer, and parses nested LLM/tool events only for a
distinct child transcript.

**Event coverage grows with the CLI.** Qwen Code 0.21.3 exposes 22 hook
events. The harness registers the 17 events represented in
`tracing/qwen/constants.py`. It does not currently register `PostToolBatch`,
`UserPromptExpansion`, `MessageDisplay`, `SessionDelete`, or
`InstructionsLoaded`. Older Qwen versions simply ignore registered event names
they do not support.

## Verification status

Exercised against a live qwen 0.21.3 session, exporting to both Arize AX and a
local Phoenix, with matching span trees on each:

| Path | Status |
| --- | --- |
| turn, model calls, tools, tokens | verified live |
| subagent invocation and `AGENT` span | verified live |
| tool failure (`status`, `errorType`) | verified live |
| todo phases | verified live |
| permission request / denied | **not exercised** — non-interactive runs self-approve |
| context compaction | **not exercised** — needs a long interactive session |

The two unexercised paths only log; they build no spans, so the blast radius is
small. They are implemented from the documented payloads and covered by unit
tests, not by a live run.

## Environment

| Variable | Effect |
| --- | --- |
| `ARIZE_TRACE_ENABLED` | set to `false` to disable tracing |
| `ARIZE_PROJECT_NAME` / `PHOENIX_PROJECT_NAME` | project override |
| `ARIZE_LOG_PROMPTS` | include prompt and completion text |
| `ARIZE_LOG_TOOL_CONTENT` | independently include tool input/output; disabled tool responses stay redacted even inside logged model input |
| `ARIZE_DRY_RUN` | build spans without sending them |
| `ARIZE_LOG_FILE` | hook log path (default `~/.arize/harness/logs/qwen.log`) |
| `QWEN_HOME` | Qwen settings root; installer uses `$QWEN_HOME/settings.json` when set |
| `QWEN_SESSION_KEY` | session key override; only used if `session_id` is absent |
