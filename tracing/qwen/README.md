# Qwen Code Tracing

Automatic [OpenInference](https://github.com/Arize-ai/openinference) tracing for
[Qwen Code](https://github.com/QwenLM/qwen-code) sessions. Spans are exported to
[Arize AX](https://arize.com) or [Phoenix](https://github.com/Arize-ai/phoenix).

Developed and verified against **qwen 0.21.3**.

## Setup

The installer prompts for your backend (Phoenix or Arize AX) and project name,
writes credentials to `~/.arize/harness/config.json`, and registers the hooks in
`~/.qwen/settings.json`.

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
└── LLM call 2 (LLM)
```

The turn — not the session — is the traced unit. Qwen Code does not emit
`SessionEnd` in one-shot mode (`qwen "prompt"`), so a session-scoped root span
would never close there. `UserPromptSubmit` opens a turn, `Stop` exports it.

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

**Continuation prompts are not new turns.** Qwen Code re-fires
`UserPromptSubmit` after tool results to continue the same turn, with an empty
`prompt`. Only a non-empty prompt opens a turn.

**Subagent internals are not published.** `agent_transcript_path` on
`SubagentStop` points at the *parent* transcript, not a child file, and Qwen
Code exposes no separate record of what ran inside the subagent. The harness
reports the invocation, its timings and its final answer — an `AGENT` span
without children. This is a limitation of the CLI, not of the harness.

**Event coverage grows with the CLI.** Qwen Code 0.15.11 has 14 hook events;
0.21.3 has 22. All are registered unconditionally — Qwen Code ignores hook
entries for events it does not know, so an older CLI simply never fires them
and produces a thinner but valid trace.

## Environment

| Variable | Effect |
| --- | --- |
| `ARIZE_TRACE_ENABLED` | set to `false` to disable tracing |
| `ARIZE_PROJECT_NAME` / `PHOENIX_PROJECT_NAME` | project override |
| `ARIZE_LOG_PROMPTS` | include prompt and completion text |
| `ARIZE_DRY_RUN` | build spans without sending them |
| `ARIZE_LOG_FILE` | hook log path (default `~/.arize/harness/logs/qwen.log`) |
| `QWEN_SESSION_KEY` | session key override; only used if `session_id` is absent |
