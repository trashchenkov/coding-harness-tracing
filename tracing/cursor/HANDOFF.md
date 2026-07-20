# Cursor high-fidelity tracing: engineering handoff

## Purpose

This document records the work performed on branch
`feat/cursor-high-fidelity-tracing` in the fork
`trashchenkov/coding-harness-tracing`, the validation already completed, the
problems found during review, and the remaining uncertainties.

It is an engineering handoff, not a claim that the branch is ready for an
upstream pull request.

## Git scope

- Branch: `feat/cursor-high-fidelity-tracing`
- Original comparison base used during development:
  `6aaf00a383671887d2ebba916e3b47df1fe7f585`
- Last code-only candidate before this handoff document:
  `b1d28bde7ac46eb7b61ebe7ad0c5efa8e7fcf732`
- Code-only candidate tree:
  `db9c622d03d88776014a4955ad88a32bbf13f958`
- Code-only branch delta: 13 files, 3,352 insertions, 329 deletions.
- The commit containing this document is intentionally documentation-only and
  will have a different SHA from the reviewed code candidate.

The branch consists of 14 code commits, from:

```text
c1b0d8b feat(cursor): add current high-fidelity hook tracing
```

through:

```text
b1d28bd fix(cursor): harden terminal lifecycle state
```

Use the full hashes from `git log` rather than relying on abbreviated hashes
when reviewing or reproducing results.

## What was implemented

### 1. Current Cursor hook surface

The hook configuration and dispatcher were updated for the documented Cursor
2.5+ event surface. The dispatcher handles:

- prompt/model lifecycle: `beforeSubmitPrompt`, `afterAgentResponse`,
  `afterAgentThought`, `stop`;
- shell/MCP/file events: `beforeShellExecution`, `afterShellExecution`,
  `beforeMCPExecution`, `afterMCPExecution`, `beforeReadFile`, `afterFileEdit`;
- Tab events: `beforeTabFileRead`, `afterTabFileEdit`;
- generic tool events: `preToolUse`, `postToolUse`, `postToolUseFailure`;
- subagents: `subagentStart`, `subagentStop`;
- application/session lifecycle: `sessionStart`, `sessionEnd`, `preCompact`,
  `workspaceOpen`.

The host may not emit every event on every Cursor build, mode, or UI surface.
The code does not infer IDE versus CLI from payload-key casing.

### 2. Span correlation and pairing

- Generic tools pair by `tool_use_id`, with digest-based state keys to avoid
  collisions between identifiers that sanitize to the same filename.
- Subagents pair using fields actually declared by the current hook contract.
  The implementation supports repeated/nested-looking starts without assuming
  an undeclared stop-side identifier.
- Correlation keys tolerate malformed JSON strings containing lone Unicode
  surrogates.
- Generation-less payloads use degraded inline behavior instead of retaining
  unbounded generation-scoped state.
- Trace/span identity and timing are kept stable across paired events.

### 3. Lifecycle and duplicate delivery

- Durable SQLite tombstones prevent replay after a generation has completed.
- `stop` and `sessionEnd` are treated as distinct once-only terminal events;
  either order can emit each event once.
- Terminal claims are domain-separated by event type.
- The terminal claim is recorded before telemetry side effects, so a process
  failure cannot replay the same terminal event on retry.
- Legacy tombstones that predate event-domain separation are ambiguous. They
  are handled fail-closed: neither terminal event is guessed/replayed.
- Generated and generation-less terminal paths have separate deterministic
  fallback identities.
- Capacity limits fail closed rather than silently admitting replay when the
  completion ledger is saturated or unavailable.

### 4. Durable state and cleanup

- State is sharded under `~/.arize/harness/state/cursor/`.
- State writes use private directory/file modes and atomic replacement.
- Reads reject symlinked final components and validate regular files,
  ownership, and link count before consuming state.
- Cleanup is confined to the expected Cursor state root and refuses symlinked
  shard paths.
- Generation cleanup is represented by a durable pending-cleanup ledger.
- Cleanup sweeps are bounded; successfully processed markers are removed while
  failed and unselected work remains durable for later retries.
- SQLite schema validation checks retained rows as well as future writes.
  Validation temporarily drops and then restores the exact original trigger
  SQL.
- Digest columns are rejected if retained rows contain non-text values,
  including SQLite `BLOB` values that can compare equal to a text digest under
  Python conversion.

### 5. Privacy behavior

Privacy switches remain independent:

```bash
ARIZE_LOG_PROMPTS=false
ARIZE_LOG_MODEL_OUTPUTS=false
ARIZE_LOG_TOOL_CONTENT=false
ARIZE_LOG_TOOL_DETAILS=false
```

Deferred content is redacted before disk-backed storage when capture is
disabled. Current privacy policy is applied again when a deferred span is
finalized, so changing policy between start and stop cannot expose content
that is now disallowed. Duplicate lifecycle delivery and cleanup do not remove
privacy tombstones needed by a later matching event.

### 6. Plugin bootstrap

`tracing/cursor/scripts/run-hook` was hardened so that the plugin:

- uses a dedicated venv rather than sharing ownership with `install.sh`;
- refreshes installation when installable Python source changes, not merely
  when `pyproject.toml` changes;
- snapshots the source hash consistently around installation;
- serializes first-fire bootstrap;
- attempts monotonic stale-lock recovery with a separate atomic reclaim claim;
- exits successfully on failures and signals so tracing cannot block Cursor;
- reserves stdout for Cursor control JSON and writes diagnostics to stderr.

### 7. Documentation and tests

- Cursor README documents plugin installation, credentials, event coverage,
  state/log paths, privacy controls, and verification limitations.
- The bundled `manage-cursor-tracing` skill was updated.
- A current-contract regression file was added.
- Tests cover pairing, duplicate delivery, privacy changes, malformed
  identities, SQLite corruption, cleanup backlog, symlinked state, bootstrap
  races, install refresh, and generation-less paths.

## Review history and defects found

The branch was not produced in one pass. Independent reviews returned blocking
findings, after which the code and tests were revised repeatedly.

### Finding: non-text SQLite digest rows survived validation

Problem:

- Validation checked future writes but did not reliably reject retained rows
  whose `digest` had SQLite type `BLOB`.
- Converting a returned value in Python was insufficient because a blob can
  preserve byte content while violating the schema's intended text identity.

Fix:

- Validate retained row storage type explicitly.
- Preserve the exact trigger SQL, temporarily remove triggers for the
  validation probe, and restore the exact definitions afterward.
- Add regression coverage for a retained BLOB digest with trigger restoration.

### Finding: private state reads followed a symlinked final component

Problem:

- Write-side checks and private permissions did not ensure that a read could
  not follow a substituted final-component symlink.

Fix:

- Open state files with no-follow semantics where supported.
- Validate regular-file type, ownership, and link count on the opened file.
- Treat unsafe/unreadable state as unavailable rather than exporting it.
- Add symlink read regressions.

### Finding: cleanup could traverse a symlinked shard directory

Problem:

- A cleanup path derived from a digest was confined lexically, but a shard
  component could still be replaced by a symlink.

Fix:

- Validate the shard path before traversal/removal.
- Refuse unsafe shard roots.
- Add cleanup confinement regressions.

### Finding: `stop` and `sessionEnd` shared one completion tombstone

Problem:

- Marking the whole generation completed for the first terminal event caused
  the other valid terminal event to be suppressed.

Fix:

- Introduce event-domain-separated terminal claims.
- Allow one `stop` and one `sessionEnd`, in either order, while suppressing
  duplicates of each.
- Keep generation-level state cleanup exactly once.
- Add generated and generation-less ordering/duplicate tests.

### Finding during final self-review: upgrade replay ambiguity

Problem:

- An old ledger has only a generation completion tombstone; it does not record
  whether `stop` or `sessionEnd` caused it.
- Treating such a record as a new-format claim would permit one guessed event
  after upgrade, which could replay telemetry.

Fix:

- If a legacy generation tombstone exists with no new event-domain claims,
  treat it as ambiguous and fail closed.
- Check the old generation-less fallback digest as well.
- Add regression tests for both generated and generation-less legacy records.

### Review-process issue: broad automated review request was refused once

One broad reviewer request was blocked by the model provider's automated
safety classifier. This was a review-tool limitation, not a repository test
failure. The review was reissued as narrowly scoped local correctness work.

## Commands and results already obtained

All commands below were run from the Cursor worktree:

```bash
cd /root/repos/coding-harness-tracing-worktrees/cursor
```

Environment isolation followed the repository/Hermes convention:

```bash
env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT=.venv uv run --frozen ...
```

### Focused Cursor suite

Before the last compatibility regression was added:

```bash
uv run --frozen pytest -q --no-cov tests/tracing/cursor
```

Result:

```text
290 passed
```

The final full repository run includes the two additional parametrized
compatibility cases.

### Final full repository suite for code SHA `b1d28bd...`

```bash
env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT=.venv \
  uv run --frozen pytest -q --no-cov
```

Result:

```text
2167 passed in 27.19s
```

### Static and formatting gates

```bash
env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT=.venv \
  uv run --frozen pre-commit run --all-files
python3 -m compileall -q tracing/cursor/hooks tests/tracing/cursor
git diff --check
```

Result:

- whitespace/end-of-file/config checks: PASS;
- isort: PASS;
- black: PASS;
- ruff: PASS;
- mypy core and every tracing integration, including Cursor: PASS;
- compileall: PASS;
- diff check: PASS.

The worktree was clean immediately after creating code candidate
`b1d28bde7ac46eb7b61ebe7ad0c5efa8e7fcf732`.

## What remains unproven or incomplete

### 1. Real Cursor host invocation was not established end to end

The tests replay handler payloads and verify plugin/bootstrap behavior, but do
not prove that a particular real Cursor IDE or `agent` CLI build emits every
registered hook for every action. This depends on the installed Cursor build,
surface, mode, and host behavior.

A local real-host smoke should record which events actually fire for:

- one prompt/response;
- shell execution;
- MCP execution;
- generic tool success and failure;
- file read/edit;
- subagent start/stop;
- stop followed by session end;
- duplicate/retried terminal delivery if it can be induced.

### 2. Real Phoenix/Arize export was not established in the final round

Unit/integration tests mock or isolate transport. The final round did not
establish a real trace in Phoenix or Arize AX and visually inspect its parent
relationships, attributes, timing, and redaction.

### 3. Cross-platform bootstrap was not exercised on every platform

The shell bootstrap has regression tests, but the final validation was run on
Linux. A real macOS GUI-launched Cursor test is especially useful because GUI
apps may not inherit shell environment variables. Windows plugin behavior
also needs a real host smoke if Windows support is required for this change.

### 4. Final independent exact-SHA reviews completed with two blockers

Two read-only reviews were launched for exact code SHA
`b1d28bde7ac46eb7b61ebe7ad0c5efa8e7fcf732`. The branch was initially pushed
before they completed because the repository owner explicitly requested an
immediate fork-branch handoff. Both reviews subsequently returned `FAIL`.
Their findings were independently reproduced in the pushed worktree.

#### Blocker A: root-state reads follow a symlinked generation shard

`gen_root_span_get()` constructs `STATE_DIR / token / root_<token>` directly.
`_read_private_text()` rejects a symlink only at the final file component; it
does not reject a symlink at the intermediate `STATE_DIR / token` shard.
Consequently, an external matching root file can be read and used as parent
span state.

Independent reproduction result:

```text
symlink_parent_read= 'EXTERNAL_PARENT_SPAN_SECRET'
```

Cleanup already rejects a symlinked shard, so read and cleanup confinement are
inconsistent. The fix should be test-driven and should validate/open the
parent shard without following symlinks rather than relying only on lexical
path construction.

#### Blocker B: `sessionEnd` before `stop` deletes a deferred Agent Response

Generated `afterAgentResponse` stores an LLM entry for later emission.
`_handle_stop()` drains that stack, but `_handle_session_end()` does not.
Dispatch nevertheless performs complete generation cleanup after either
terminal event. If `sessionEnd` arrives first, cleanup removes the pending LLM
entry; a later `stop` cannot emit it.

Independent reproduction result for
`afterAgentResponse -> sessionEnd -> duplicate sessionEnd -> stop -> duplicate stop`:

```text
before_terminal= ['User Prompt']
after_terminal= ['User Prompt', 'Session End', 'Agent Stop']
agent_response_present= False
```

The expected `Agent Response` is absent. Existing ordering coverage exercised
`stop -> sessionEnd`, not the inverse state-loss path.

These blockers mean the branch must not be presented as ready for an upstream
pull request despite the green 2,167-test suite. No code fix for either finding
was included in this documentation update.

**Resolution (continuation round, 2026-07-19):** both blockers were fixed in a
later round; see "Continuation round" below. Blocker A: root-state reads now
open the generation shard with no-follow semantics and read the root file
relative to that descriptor; private reads additionally validate ownership and
refuse hard-linked files. Blocker B: `_handle_session_end` shares one flush
helper with `_handle_stop`, so whichever terminal event arrives first emits the
deferred Agent Response entries. Regression tests cover the symlinked shard,
the hard-linked file, the `sessionEnd -> stop` ordering with duplicates, and
token routing in that ordering.

### 5. No upstream PR was created

This branch is only a handoff branch in the fork. It was not pushed to
`Arize-ai/coding-harness-tracing`, no upstream PR was opened, and no claim is
made that upstream maintainers have reviewed it.

## How to fetch and inspect

```bash
git clone git@github.com:trashchenkov/coding-harness-tracing.git
cd coding-harness-tracing
git fetch origin feat/cursor-high-fidelity-tracing
git switch --track origin/feat/cursor-high-fidelity-tracing
```

If the repository is already cloned:

```bash
git fetch origin feat/cursor-high-fidelity-tracing
git switch feat/cursor-high-fidelity-tracing
# If the local branch does not exist:
# git switch --track origin/feat/cursor-high-fidelity-tracing
```

Inspect history and branch delta:

```bash
git log --oneline --decorate --reverse \
  6aaf00a383671887d2ebba916e3b47df1fe7f585..HEAD
git diff --stat 6aaf00a383671887d2ebba916e3b47df1fe7f585..HEAD
git diff 6aaf00a383671887d2ebba916e3b47df1fe7f585..HEAD -- tracing/cursor
```

Run validation:

```bash
env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT=.venv uv sync --frozen
env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT=.venv \
  uv run --frozen pytest -q --no-cov tests/tracing/cursor
env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT=.venv \
  uv run --frozen pytest -q --no-cov
env -u VIRTUAL_ENV UV_PROJECT_ENVIRONMENT=.venv \
  uv run --frozen pre-commit run --all-files
```

## Suggested local investigation order

1. Read `tracing/cursor/hooks/hooks.json` and compare it with the exact Cursor
   build's current hook documentation.
2. Read `tests/tracing/cursor/test_cursor_current_contract.py`; it is the
   shortest map from current hook assumptions to executable examples.
3. Inspect `_dispatch()` in `tracing/cursor/hooks/handlers.py`, then the
   completion ledger and private-state primitives in `adapter.py`.
4. Run only `tests/tracing/cursor` before changing code.
5. Install the branch into an isolated test workspace and use a local Phoenix
   instance or a dry-run transport capture.
6. Record raw event names and non-sensitive payload keys from the actual host;
   do not infer unavailable fields.
7. Exercise stop/session-end ordering and restart the hook process between
   events to test durable behavior, not only in-process behavior.
8. Change each privacy variable between start and stop and inspect both state
   files and exported attributes.
9. Only after host and backend evidence is captured, decide whether the branch
   is suitable for an upstream PR or needs contract changes.

## Files most relevant to continue the work

- `tracing/cursor/hooks/hooks.json` — registered events.
- `tracing/cursor/hooks/handlers.py` — dispatch and span construction.
- `tracing/cursor/hooks/adapter.py` — durable ledger, state files, locking,
  cleanup, transport helpers.
- `tracing/cursor/scripts/run-hook` — plugin venv/bootstrap dispatcher.
- `tests/tracing/cursor/test_cursor_current_contract.py` — current contract
  assumptions.
- `tests/tracing/cursor/test_cursor_hook.py` — lifecycle/privacy/pairing.
- `tests/tracing/cursor/test_cursor_adapter.py` — durable-state primitives.
- `tests/tracing/cursor/test_cursor_plugin.py` — plugin bootstrap behavior.
- `tracing/cursor/README.md` — user-facing setup and limitations.

## Continuation round (2026-07-19, macOS real-host smoke)

A later round on macOS 24.6.0 (Darwin, arm64) fixed both blockers, fixed test
portability, and performed the first real-host smoke with the Cursor CLI.

### Code changes

- Blocker A fixed: `gen_root_span_get()` reads through
  `_read_private_shard_text()`, which opens the shard directory with
  `O_NOFOLLOW|O_DIRECTORY` and opens the root file via `dir_fd`, so a
  symlinked intermediate shard can no longer substitute parent span state.
  Private file reads now also reject foreign ownership and link counts > 1.
- Blocker B fixed: `_handle_stop` and `_handle_session_end` share
  `_flush_deferred_llm_spans()`. Whichever terminal event arrives first emits
  the deferred Agent Response entries and routes payload tokens to the most
  recent LLM span, falling back to the terminal CHAIN span when no entries
  exist.
- Bootstrap fix found on the real host: with macOS CommandLineTools Python
  3.9, the venv pip is 21.2.4, which builds from a temp copy of the tree and
  breaks the relative `core` symlink (`error: package directory 'core' does
  not exist`), so the plugin entry point silently never installed. `run-hook`
  now retries `pip install` with `--use-feature=in-tree-build` when the plain
  install fails.
- Test portability: the fake bootstrap pip/python/rm scripts hardcoded
  `/usr/bin` utility paths that only exist on usr-merged Linux; on macOS the
  parallel-bootstrap and stale-lock tests failed. The fakes now resolve real
  binaries and exec the running interpreter.

### Validation on macOS

- Full repository suite: 2,172 passed. Cursor suite: 297 passed (was 290
  passed / 2 failed on macOS before the portability fix). All pre-commit
  gates, compileall, and `git diff --check` pass.
- Real-host smoke: Cursor CLI (`agent` 2026.05.16, print mode with hooks in
  the workspace `.cursor/hooks.json`) against a local HTTP capture sink via
  `PHOENIX_ENDPOINT`. First-fire venv bootstrap, dispatch, state, ledger
  claims, and span export all worked end to end after the pip fix.

### Real-host observations (Cursor CLI, print mode)

- Events observed: `workspaceOpen`, `sessionStart`, `beforeShellExecution`,
  `afterShellExecution`, `beforeReadFile`, `afterFileEdit`, `preToolUse`,
  `postToolUse`, `afterAgentThought`, `sessionEnd`.
- Events NOT observed on this surface: `beforeSubmitPrompt`,
  `afterAgentResponse`, `stop`, MCP events, Tab events, subagent events,
  `preCompact`. In particular, `sessionEnd` is the only terminal event on this
  surface — the sessionEnd-first path fixed in Blocker B is the path this host
  actually takes.
- Correlation: session, shell, read, and edit events shared one
  generation-derived trace and parented correctly under the Session Start
  root. `afterAgentThought` payloads carried a different generation identity
  per thought, so each Agent Thinking span landed in its own parentless
  trace. Whether to re-key thoughts to the conversation trace is a contract
  decision left open; record IDE payloads before changing it.
- Privacy: with `ARIZE_LOG_TOOL_CONTENT=false`, shell output was exported as
  `<redacted (0 chars)>` while the command text stayed visible — command text
  is governed by `ARIZE_LOG_TOOL_DETAILS`, matching the documented split.

### Real-host observations (Cursor IDE 3.8.24, macOS GUI)

A second smoke exercised the GUI IDE with workspace-level hooks and
credentials supplied only via `~/.arize/harness/config.json` (no env vars).

- The deferred prompt/response pipeline that the CLI never exercises worked
  end to end: `beforeSubmitPrompt` deferred the root, `afterAgentResponse`
  emitted the User Prompt root and deferred the LLM entry, and `stop` emitted
  the Agent Response LLM span carrying the per-turn token counts (including
  the cache_read/cache_write split) plus the Agent Stop CHAIN — all in one
  generation-derived trace with correct parenting.
- Events observed: `beforeSubmitPrompt`, `afterAgentResponse`,
  `afterAgentThought`, `beforeShellExecution`, `afterShellExecution`,
  `beforeReadFile`, `afterFileEdit`, `preToolUse`, `postToolUse`,
  `sessionStart`, `stop`.
- Events NOT observed on this surface: `sessionEnd` — not even when the
  workspace window was closed — plus `workspaceOpen`, MCP events, Tab
  events, subagent events, `preCompact`, `postToolUseFailure`. The IDE's
  terminal event on this build is `stop`; the CLI's is `sessionEnd`. Both
  orderings are therefore reachable in production, which is why the Blocker B
  flush-on-either-terminal fix matters on both surfaces.
- The `afterAgentThought` identity split reproduced here too: 3 of 7 thought
  events carried the turn's generation_id (parented correctly), 4 carried a
  different generation identity and landed in parentless traces.
- The bounded-cleanup design was validated live: the turn accumulated more
  private state entries than one 16-entry cleanup pass allows, `stop`-time
  cleanup failed loudly with "cleanup incomplete; retry required" after the
  terminal claim was already durable (no replay), later hook fires swept the
  backlog through the pending-cleanup ledger, and a final probe event removed
  the last marker and the shard.
- The `config.json` credentials route for GUI-launched Cursor works as
  documented.

### Still open after this round

- Export verified against a local capture sink in Phoenix payload shape; a
  real Phoenix/Arize UI inspection of parent relationships has still not been
  performed.
- Windows bootstrap remains unexercised.
- MCP, Tab, subagent, `preCompact`, and `postToolUseFailure` events were not
  triggered on either real surface; their handlers remain validated only by
  payload replay.
- No upstream PR decision was made in this round.

## Review response round (2026-07-19, after independent re-review)

Independent re-review of the continuation round returned REQUEST CHANGES with
three blocking findings sharing one root cause: the completion ledger
distinguishes the two terminal events, but generation state died after the
first one. All findings were addressed:

- **Second terminal span lost its parent** (both orders): the first terminal
  claim now persists the root span identity in a content-free
  `terminal_attribution` ledger table (digest-keyed, retained like
  tombstones), and `_terminal_attribution_context()` recovers the parent when
  the root state file is already cleaned up.
- **Cumulative tokens double-counted**: usage is attributed exactly once per
  generation. The first terminal event with token data attaches it (to the
  flushed Agent Response, else to its own CHAIN span) and durably sets
  `usage_attributed`; a later terminal event never re-attaches counts. A
  tokenless first terminal does not burn the attribution — a later terminal
  with counts still carries them. The earlier regression test that codified
  the double-count was replaced by a full two-order matrix test asserting
  span names, parent linkage, trace identity, single token ownership, model
  attribution, duplicate suppression, and cleanup.
- **`sessionEnd`-first lost `llm.model_name`**: `_handle_session_end` now
  parses `model` and routes it with the token attrs, mirroring `stop`.
- **TOCTOU fallback** (medium): on platforms without `dir_fd` support the
  shard read now fails closed (state unavailable) instead of a check-then-open
  race.
- **Old-pip fallback range** (medium): `--use-feature=in-tree-build` retry
  (pip 21.1–21.2 only) was replaced by installing from a symlink-resolved
  copy of the plugin tree, which works on every supported pip including 20.x.
  Re-validated on the real macOS host with pip 21.2.4: the plugin reinstalled
  through the new fallback and exported spans end to end.
- **Test shims now `shlex.quote` the interpreter path** (low).
- The `manage-cursor-tracing` skill's lifecycle/token wording was updated to
  the either-order terminal contract.

Suite after this round: 2,176 passed on macOS; pre-commit, compileall, and
diff checks clean. The reviewer-confirmed pre-existing issue (terminal claim
recorded before export success, so a transient export failure suppresses
redelivery) is unchanged; it predates this branch's continuation rounds and
needs its own decision.

## MCP real-host smoke (2026-07-19, same host)

A local stdio MCP server (one echo tool) was registered via the workspace
`.cursor/mcp.json`, and the Cursor CLI was driven to call it. Two more real
defects were found and fixed.

### Observations

- `beforeMCPExecution` / `afterMCPExecution` fire on the real CLI; the
  `MCP: <tool>` span pairs carry tool name, arguments, output, and correct
  parenting. MCP is no longer an unexercised event family.
- Even under `--trust`, MCP calls required separate approval; rejected calls
  still fire both hook events with the rejection recorded in the output —
  tracing captures denied MCP attempts.

### Defect: duplicate spans per MCP call

The host names MCP calls `MCP:<tool>` in the generic `preToolUse` /
`postToolUse` events while also emitting the dedicated MCP pair, so every
call produced both `MCP: echo_marker` and `Tool: MCP:echo_marker`. The
dedicated-handler suppression only matched exact names (`mcp`,
`mcp_execution`). Fixed with an `mcp:`-prefix match; re-verified live —
exactly one span per call.

### Defect: stale builds installed silently with a fresh marker

While validating the fix, the reinstalled plugin still ran old code. Root
cause chain: an earlier failed old-pip install had littered the plugin dir
with `build/` and `cursor_tracing.egg-info`; the symlink-resolved copy
carried them along; setuptools packaged the stale `build/lib` sources
instead of the current tree; and the marker still recorded the new hash —
silently pinning outdated code across refreshes. Fixed by always
installing from a pruned symlink-resolved copy (`build/` and `*.egg-info`
removed) and excluding build artifacts from the source fingerprint. Fresh
installs no longer build inside the plugin dir; legacy artifacts left by
older failed installs are not deleted — they are neutralized by the
fingerprint/pruning exclusions instead. Regression test seeds a stale
`build/lib` and asserts the install source is pruned and the marker matches
the clean-tree hash. Re-verified live: fresh bootstrap installs current
code and creates no new artifacts in the plugin dir.

### Still unexercised on a real host

Tab events (needs real IDE Tab completion), subagent events, `preCompact`
(needs a compaction-length conversation), `postToolUseFailure`, Windows,
and a real Phoenix/Arize UI inspection.

## Real Phoenix inspection (2026-07-19, local instance)

A local Phoenix (arize-phoenix, `localhost:6006`) received a real CLI run
plus an IDE-style event sequence replayed through the installed dispatcher
(beforeSubmitPrompt → thought → shell pair → afterFileEdit →
afterAgentResponse → stop → sessionEnd with token counts). Verified via
Phoenix's GraphQL API and visually in the UI:

- **Ingestion**: all 16 span POSTs to `/v1/projects/cursor/spans` returned
  202; zero rejections in the server log — the payload shape is accepted by
  the real backend, not just a capture stub.
- **Topology**: the deferred-pipeline trace assembled as one tree — User
  Prompt root (with real 2.3s latency from the deferred start time), Agent
  Thinking / Shell / File Edit / Agent Response / Agent Stop / Session End
  all correctly parented, confirming both terminal spans keep their parent
  after the terminal-attribution fix. No missing-parent references anywhere.
- **OpenInference semantics**: the Agent Response span renders as a proper
  LLM span — model `composer-2` shown, LLM Input/Output panes populated,
  token count 2,390 displayed and aggregated into the project total
  (2,050 prompt including cache buckets + 340 completion). CHAIN/TOOL kinds
  render with their badges; paired tool spans show real latencies.
- **Sessions**: the Sessions view groups both conversations by
  `session.id`, shows first input / last output, and surfaces `user.id`.
- **Confirmed UX cost of the known thought-identity issue**: the orphan
  Agent Thinking spans appear as separate single-span parentless traces in
  the root-spans list — visible clutter, reinforcing that the
  `afterAgentThought` generation-identity contract question is worth
  resolving before wide rollout.
- **Notes**: `ARIZE_PROJECT_NAME` is intentionally ignored on the Phoenix
  backend (framework-scoped project override is `PHOENIX_PROJECT_NAME`;
  see issue #74), so spans land in the service-name project `cursor`.
  Total Cost shows $0 because Phoenix has no pricing for `composer-2` —
  cost requires a model-pricing entry, not a tracing change.

Arize AX remains unexercised end to end (needs real credentials): its
OTLP JSON transport, auth headers, and `arize.project.name` injection have
never run against the real service.

## Event-coverage closure round (2026-07-20, real hosts)

A final round closed the remaining event families that could be exercised
locally.

- **`postToolUseFailure` — validated live.** A deliberately invalid Grep
  regex on the CLI fired the event; the span carries the rg error output,
  `cursor.tool.status=error`, and `cursor.tool.failure_type`. This smoke
  also exposed a defect: failure spans were exported with OTLP status OK,
  so backend error metrics (e.g. Phoenix "spans by status") never counted
  them. Fixed: `postToolUseFailure` spans now carry OTLP status ERROR with
  the privacy-redacted error message (matches the omp/opencode convention);
  regression added.
- **Tab events — validated live.** Real typing with Cursor Tab completions
  in the IDE fired `beforeTabFileRead` (12×) and `afterTabFileEdit` (3×);
  exported spans carry the file path and exact edit ranges/diffs. Note: Tab
  events have no generation context, so Tab spans are parentless in their
  own trace — Tab activity is not part of an agent turn.
- **Subagent events — the host does not emit them.** Even when the CLI
  demonstrably used its Task tool to spawn a subagent (forced via prompt;
  the subagent's own read/shell actions flowed through the session's normal
  hooks), no `subagentStart`/`subagentStop` was delivered, and the Task
  call's `preToolUse` never received a closing `postToolUse`. The subagent
  handlers remain validated by payload replay only; this is a host contract
  gap for maintainers, not an integration defect. Models also happily
  *claim* subagent use without emitting anything — do not trust prompt
  output as evidence here.
- **`preCompact` — not deterministically triggerable.** `/compact`,
  `/compress`, and `/summarize` in the interactive TUI do not fire it, and
  print mode treats them as prompts. Forcing a real compaction requires
  filling the context window, which is expensive and non-deterministic.
  The handler remains validated by payload replay only.

Per an explicit scoping decision, Arize AX and Windows are left to the
upstream maintainers.

## Second review response (2026-07-20)

An independent review of SHA `a5be1b3` returned REQUEST CHANGES against the
MCP de-duplication and the bootstrap regression test. All findings fixed:

- **P1: name-prefix suppression could drop generic-only MCP telemetry.**
  Suppression is now delivery-aware: `afterMCPExecution` records a
  content-free per-generation marker only when it actually emitted the
  dedicated span, and the generic `postToolUse` / `postToolUseFailure`
  handlers suppress only by consuming that marker. A surface delivering
  only generic events keeps full MCP telemetry; generation-less payloads
  are never suppressed.
- **P1: the failure path could still duplicate.** `postToolUseFailure` now
  consumes the same marker (after popping generic state, so no dangling
  files). When the after payload declares an error, the surviving dedicated
  span itself carries OTLP ERROR status, `cursor.tool.status=error`, and
  the redacted message. The reviewer's full matrix is encoded as a
  regression test: generic-only success, dedicated+generic success,
  generic-only failure, dedicated+generic failure, and the observed denied
  case — one span each, with correct status.
- **P2: the bootstrap regression test failed on a polluted source tree**
  (exactly the state it protects against). Fixed with `exist_ok` mkdirs and
  re-verified with `build/` + `*.egg-info` seeded into the plugin tree.
  The "plugin dir stays clean" claim was narrowed: fresh installs create no
  artifacts, but legacy artifacts are neutralized, not deleted.

Residual limitation, documented deliberately: if a host emits a
successful-looking `afterMCPExecution` and only signals the failure in the
later `postToolUseFailure`, the failure detail lands in logs, not on the
already-sent dedicated span. No observed surface does this; revisit only
with host evidence.

## Safety and delivery note

Do not push this branch directly to upstream or merge it into a default branch
based only on the local green suite. Preserve the branch as an inspectable
artifact, complete the real-host/backend smoke, examine the pending exact-SHA
review results, and then make a separate decision about any pull request.
