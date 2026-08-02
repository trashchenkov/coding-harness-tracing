"""Qwen Code hook handlers. One exported function per hook event.

Each function is a CLI entry point registered in pyproject.toml
[project.scripts]. Hooks must never fail the host process, so every entry point
swallows its exceptions after logging them.

The turn, not the session, is the unit of tracing — the same choice
``tracing/claude_code`` makes. ``UserPromptSubmit`` opens a turn and ``Stop``
exports it; ``SessionEnd`` only cleans up. This matters because Qwen Code does
not emit ``SessionEnd`` at all in one-shot (``qwen "prompt"``) mode, so a
session-scoped root span would never close there.
"""

import json
import sys

from core.common import (
    env,
    error,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
    redact_content,
    send_span,
)
from core.event_model import AgentEvent, EventStatus, ToolEvent, TurnEvent
from core.span_renderer import render_event_graph

from tracing.qwen.constants import TODO_PHASE_POSTWRITE

from .adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_session,
    resolve_transcript_path,
)
from .transcript import parse_qwen_transcript


def _read_stdin() -> dict:
    """Read hook input JSON from stdin, tolerating empty or malformed payloads."""
    try:
        raw = sys.stdin.read()
    except (OSError, UnicodeError):
        return {}
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def _handle_session_start(input_json: dict) -> None:
    """Initialize session state. SessionStart also carries the resolved model."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    model = input_json.get("model") or ""
    if model:
        state.set("model", model)
    source = input_json.get("source") or ""
    if source:
        state.set("session_source", source)
    log(f"Session started: {state.get('session_id')} (source={source or 'unknown'})")


def _handle_session_end(input_json: dict) -> None:
    """Close out a session. Exports any turn Stop did not close, then cleans up."""
    state = resolve_session(input_json)
    if state.get("current_trace_id"):
        fallback = dict(input_json)
        fallback.setdefault("last_assistant_message", "(Turn closed by fail-safe: Stop hook did not fire)")
        _handle_stop(fallback)
    trace_count = state.get("trace_count") or "0"
    tool_count = state.get("tool_count") or "0"
    error(f"Session complete: {trace_count} traces, {tool_count} tools")
    error(f"View in Arize/Phoenix: session.id = {state.get('session_id')}")

    if state.state_file is not None:
        state.state_file.unlink(missing_ok=True)
    if state._lock_path is not None and state._lock_path.is_dir():
        try:
            state._lock_path.rmdir()
        except OSError:
            pass
    gc_stale_state_files()


# ---------------------------------------------------------------------------
# Turn lifecycle
# ---------------------------------------------------------------------------


def _handle_user_prompt_submit(input_json: dict) -> None:
    """Open a new turn, closing an orphaned one first."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)

    # A retained turn means a previous export failed; retry it before opening a
    # new one so stable span IDs and the transcript offset are not lost.
    prev_trace_id = state.get("current_trace_id") or ""
    if prev_trace_id:
        retry_input = dict(input_json)
        retry_input.setdefault("last_assistant_message", "(Turn closed by fail-safe: Stop hook did not fire)")
        _handle_stop(retry_input)
        current = state.get("current_trace_id") or ""
        if current == prev_trace_id:
            log("Fail-safe: retained orphaned turn after failed export")
            return
        if current:
            log("Fail-safe: a concurrent newer turn owns the session state")
            return

    state.increment("trace_count")
    state.set("current_trace_id", generate_trace_id())
    state.set("current_trace_span_id", generate_span_id())
    state.set("current_trace_start_time", str(get_timestamp_ms()))
    # Store the raw prompt; redaction happens at emit time so the toggle is read
    # once per export rather than baked into the state file.
    state.set("current_trace_prompt", input_json.get("prompt", "") or "")
    state.set("pending_subagents", "{}")

    # Record where this turn starts in the transcript so Stop only reads the
    # lines that belong to it.
    transcript = resolve_transcript_path(input_json)
    if transcript is not None:
        try:
            with transcript.open(encoding="utf-8") as fh:
                line_count = sum(1 for _ in fh)
        except (OSError, UnicodeError):
            line_count = 0
        state.set("trace_start_line", str(line_count))
    else:
        state.set("trace_start_line", "0")


def _handle_stop(input_json: dict) -> None:
    """Export the completed turn as an OpenInference span tree."""
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    trace_id = state.get("current_trace_id")
    # Empty strings can survive in state files written by older versions; treat
    # them as absent so a cleared turn is never re-exported with a blank trace.
    if not session_id or not trace_id:
        return

    trace_span_id = state.get("current_trace_span_id") or generate_span_id()
    trace_start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    user_prompt = state.get("current_trace_prompt") or ""
    project_name = state.get("project_name") or ""
    trace_count = state.get("trace_count") or "0"
    user_id = state.get("user_id") or ""
    ended_at = get_timestamp_ms()

    root_event = TurnEvent(
        event_id=f"turn-{trace_id}",
        session_id=session_id,
        turn_id=trace_id,
        sequence=0,
        started_at_ms=int(trace_start_time),
        ended_at_ms=ended_at,
        status=EventStatus.COMPLETED,
        input=user_prompt,
        output=input_json.get("last_assistant_message", "") or "",
    )

    transcript = resolve_transcript_path(input_json, session_id)
    if transcript is None:
        log("Stop: no transcript available; nothing to export")
        _clear_turn(state)
        return

    start_line = int(state.get("trace_start_line") or "0")
    graph = parse_qwen_transcript(transcript, root_event, start_line=start_line)
    _merge_subagents(state, graph)

    root_attrs = {
        "session.id": session_id,
        "trace.number": trace_count,
        "project.name": project_name,
        "input.value": redact_content(env.log_prompts, user_prompt),
    }
    model = state.get("model") or ""
    if model:
        root_attrs["llm.model_name"] = model
    if user_id:
        root_attrs["user.id"] = user_id

    # Span IDs are persisted so a retried export reuses the same identities
    # instead of duplicating the turn under fresh IDs.
    span_id_overrides = {root_event.event_id: trace_span_id}
    stored = state.get("qwen_span_ids") or ""
    if stored:
        try:
            decoded = json.loads(stored)
            if isinstance(decoded, dict):
                span_id_overrides.update(
                    {str(k): str(v) for k, v in decoded.items() if isinstance(k, str) and isinstance(v, str) and v}
                )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log(f"invalid qwen_span_ids state; regenerating span IDs: {exc}")
    for event in graph.events:
        span_id_overrides.setdefault(event.event_id, generate_span_id())
    state.set("qwen_span_ids", json.dumps(span_id_overrides, sort_keys=True))

    payload = render_event_graph(
        graph,
        trace_id=trace_id,
        service_name=SERVICE_NAME,
        scope_name=SCOPE_NAME,
        span_id_overrides=span_id_overrides,
        extra_attributes={root_event.event_id: root_attrs},
    )
    if send_span(payload) is False:
        # Keep the turn so the next UserPromptSubmit or SessionEnd can retry it.
        return

    _clear_turn(state)
    _periodic_gc(trace_count)


def _handle_stop_failure(input_json: dict) -> None:
    """A turn that ended in failure still deserves a span; mark it as such."""
    state = resolve_session(input_json)
    if state.get("current_trace_id") is None:
        return
    state.set("turn_failed", "1")
    _handle_stop(input_json)


def _clear_turn(state) -> None:
    """Drop per-turn state once a turn has been exported."""
    for key in (
        "current_trace_id",
        "current_trace_span_id",
        "current_trace_start_time",
        "current_trace_prompt",
        "trace_start_line",
        "qwen_span_ids",
        "pending_subagents",
        "turn_failed",
    ):
        state.delete(key)


def _periodic_gc(trace_count: str) -> None:
    """Sweep stale PID-keyed state files every tenth turn."""
    try:
        if int(trace_count) % 10 == 0:
            gc_stale_state_files()
    except (TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Subagents
# ---------------------------------------------------------------------------


def _handle_subagent_start(input_json: dict) -> None:
    """Record that a subagent began, keyed by the id Qwen assigns it."""
    state = resolve_session(input_json)
    agent_id = input_json.get("agent_id") or ""
    if not agent_id:
        return
    pending = _pending_subagents(state)
    pending[agent_id] = {
        "agent_id": agent_id,
        "agent_type": input_json.get("agent_type") or "",
        "started_at_ms": get_timestamp_ms(),
    }
    state.set("pending_subagents", json.dumps(pending, sort_keys=True))


def _handle_subagent_stop(input_json: dict) -> None:
    """Record the finished subagent along with the transcript Qwen wrote for it.

    Claude Code marks subagent records inline in the parent transcript with
    ``isSidechain``. Qwen Code instead writes the child to its own file and
    hands the path over here, so the child is parsed separately at Stop.
    """
    state = resolve_session(input_json)
    agent_id = input_json.get("agent_id") or ""
    if not agent_id:
        return
    pending = _pending_subagents(state)
    entry = pending.get(agent_id, {"agent_id": agent_id})
    entry.update(
        {
            "agent_type": input_json.get("agent_type") or entry.get("agent_type") or "",
            "ended_at_ms": get_timestamp_ms(),
            "transcript_path": input_json.get("agent_transcript_path") or "",
            "output": input_json.get("last_assistant_message") or "",
        }
    )
    pending[agent_id] = entry
    state.set("pending_subagents", json.dumps(pending, sort_keys=True))


def _pending_subagents(state) -> dict:
    """Decode the pending-subagent ledger, tolerating malformed state."""
    raw = state.get("pending_subagents") or ""
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _merge_subagents(state, graph) -> None:
    """Attach each finished subagent as an AGENT subtree under its tool call.

    The parent transcript already carries the invoking tool call; the child's
    own transcript supplies the model calls and tools that ran inside it.
    """
    pending = _pending_subagents(state)
    if not pending:
        return

    # Tool calls that invoked a subagent are recoverable from the parent
    # transcript: the parser records the subagent name on `source_id`.
    agent_tools = [e for e in graph.events if isinstance(e, ToolEvent) and e.source_id]
    sequence = max((e.sequence for e in graph.events), default=0) + 1

    for index, descriptor in enumerate(sorted(pending.values(), key=lambda d: d.get("started_at_ms") or 0)):
        agent_id = descriptor.get("agent_id") or ""
        if not agent_id:
            continue
        parent_tool = agent_tools[index] if index < len(agent_tools) else None
        parent_event_id = parent_tool.event_id if parent_tool is not None else graph.events[0].event_id

        agent_event = AgentEvent(
            event_id=f"agent-{agent_id}",
            session_id=graph.events[0].session_id,
            turn_id=graph.events[0].turn_id,
            parent_event_id=parent_event_id,
            agent_id=agent_id,
            source_id=descriptor.get("agent_type") or "",
            sequence=sequence,
            started_at_ms=descriptor.get("started_at_ms"),
            ended_at_ms=descriptor.get("ended_at_ms"),
            status=EventStatus.COMPLETED,
            output=descriptor.get("output") or "",
        )
        graph.events.append(agent_event)
        sequence += 1

        # Qwen Code does not expose the subagent's internal steps: as of 0.21.3
        # `agent_transcript_path` on SubagentStop is the *parent* transcript,
        # not a child file. Parsing it again would duplicate the parent's model
        # calls and tools beneath the AGENT span. What the harness can report is
        # the invocation itself, its timings and its final answer.


# ---------------------------------------------------------------------------
# Tools, todos and advisory events
# ---------------------------------------------------------------------------


def _handle_pre_tool_use(input_json: dict) -> None:
    """Count tool starts. The transcript remains the authoritative record."""
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    state.increment("tool_count")


def _handle_post_tool_use(input_json: dict) -> None:
    """No-op beyond diagnostics: tool results are read from the transcript."""
    log(f"tool completed: {input_json.get('tool_name') or 'unknown'}")


def _handle_post_tool_use_failure(input_json: dict) -> None:
    """Log a tool failure; the transcript carries error and errorType."""
    log(f"tool failed: {input_json.get('tool_name') or 'unknown'}")


def _handle_todo_created(input_json: dict) -> None:
    """Count created todos, once per item.

    Todo hooks fire twice per item — once in the ``validation`` phase, which
    exists to block the write, and once in ``postWrite``. Counting both would
    double every todo, so only the post-write phase is durable.
    """
    if input_json.get("phase") != TODO_PHASE_POSTWRITE:
        return
    state = resolve_session(input_json)
    state.increment("todo_created_count")


def _handle_todo_completed(input_json: dict) -> None:
    """Count completed todos, once per item. See _handle_todo_created."""
    if input_json.get("phase") != TODO_PHASE_POSTWRITE:
        return
    state = resolve_session(input_json)
    state.increment("todo_completed_count")


def _handle_pre_compact(input_json: dict) -> None:
    """Note an imminent context compaction."""
    log("context compaction starting")


def _handle_post_compact(input_json: dict) -> None:
    """Note a completed context compaction."""
    log("context compaction finished")


def _handle_notification(input_json: dict) -> None:
    """Log a user-facing notification."""
    log(f"notification: {input_json.get('notification_type') or ''} {input_json.get('message') or ''}".strip())


def _handle_permission_request(input_json: dict) -> None:
    """Log a permission prompt."""
    log(f"permission requested for {input_json.get('tool_name') or 'unknown'}")


def _handle_permission_denied(input_json: dict) -> None:
    """Log a denied permission."""
    log(f"permission denied for {input_json.get('tool_name') or 'unknown'}")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _entry(name: str, handler) -> None:
    """Run *handler* against stdin, never propagating a failure to the host."""
    try:
        handler(_read_stdin())
    except Exception as exc:  # noqa: BLE001 - hooks must not break the CLI
        error(f"{name} hook failed: {exc}")


def session_start() -> None:
    _entry("session_start", _handle_session_start)


def session_end() -> None:
    _entry("session_end", _handle_session_end)


def user_prompt_submit() -> None:
    _entry("user_prompt_submit", _handle_user_prompt_submit)


def pre_tool_use() -> None:
    _entry("pre_tool_use", _handle_pre_tool_use)


def post_tool_use() -> None:
    _entry("post_tool_use", _handle_post_tool_use)


def post_tool_use_failure() -> None:
    _entry("post_tool_use_failure", _handle_post_tool_use_failure)


def stop() -> None:
    _entry("stop", _handle_stop)


def stop_failure() -> None:
    _entry("stop_failure", _handle_stop_failure)


def subagent_start() -> None:
    _entry("subagent_start", _handle_subagent_start)


def subagent_stop() -> None:
    _entry("subagent_stop", _handle_subagent_stop)


def pre_compact() -> None:
    _entry("pre_compact", _handle_pre_compact)


def post_compact() -> None:
    _entry("post_compact", _handle_post_compact)


def notification() -> None:
    _entry("notification", _handle_notification)


def permission_request() -> None:
    _entry("permission_request", _handle_permission_request)


def permission_denied() -> None:
    _entry("permission_denied", _handle_permission_denied)


def todo_created() -> None:
    _entry("todo_created", _handle_todo_created)


def todo_completed() -> None:
    _entry("todo_completed", _handle_todo_completed)
