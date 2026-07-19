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

### 4. Final independent exact-SHA reviews were still running at push request

Two read-only reviews were launched for exact code SHA
`b1d28bde7ac46eb7b61ebe7ad0c5efa8e7fcf732`:

1. durable state, SQLite, cleanup, locking, and legacy compatibility;
2. functional lifecycle, host contracts, generation-less behavior, privacy,
   bootstrap, and test validity.

The branch is being pushed before those reviews finish because the repository
owner explicitly requested an immediate fork-branch handoff. Their eventual
results therefore must not be described as pre-push approval.

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

## Safety and delivery note

Do not push this branch directly to upstream or merge it into a default branch
based only on the local green suite. Preserve the branch as an inspectable
artifact, complete the real-host/backend smoke, examine the pending exact-SHA
review results, and then make a separate decision about any pull request.
