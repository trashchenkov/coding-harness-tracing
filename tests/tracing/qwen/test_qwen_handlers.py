"""Qwen Code hook handlers.

Several of these are regression tests for defects a live 0.21.3 session
surfaced; each names the behaviour it pins down.
"""

import json

import pytest

from tracing.qwen.hooks import handlers


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point harness state at a temp directory and stop spans from being sent."""
    monkeypatch.setattr(handlers, "STATE_DIR", tmp_path, raising=False)
    monkeypatch.setattr("tracing.qwen.hooks.adapter.STATE_DIR", tmp_path)
    sent: list = []
    monkeypatch.setattr(handlers, "send_span", lambda payload: sent.append(payload) or True)
    return sent


def _payload(**overrides) -> dict:
    base = {
        "session_id": "sess-1",
        "cwd": "/tmp/project",
        "hook_event_name": "UserPromptSubmit",
        "transcript_path": "",
    }
    base.update(overrides)
    return base


def _state(payload: dict):
    return handlers.resolve_session(payload)


# ---------------------------------------------------------------------------
# Turn boundaries
# ---------------------------------------------------------------------------


def test_prompted_event_opens_a_turn(state_dir):
    payload = _payload(prompt="build the thing")

    handlers._handle_user_prompt_submit(payload)

    assert _state(payload).get("current_trace_id")


def test_continuation_event_does_not_open_a_second_turn(state_dir):
    """Regression: Qwen re-fires UserPromptSubmit with an empty prompt after
    tool results. Opening a turn on it split one request across two traces."""
    payload = _payload(prompt="build the thing")
    handlers._handle_user_prompt_submit(payload)
    first = _state(payload).get("current_trace_id")

    handlers._handle_user_prompt_submit(_payload(prompt=""))
    handlers._handle_user_prompt_submit(_payload(prompt="   "))

    state = _state(payload)
    assert state.get("current_trace_id") == first
    assert state.get("trace_count") == "1"


def test_cleared_turn_is_not_exported_again(state_dir):
    """Regression: turn keys were blanked rather than deleted, so a cleared
    turn passed an `is None` check and re-exported with an empty trace id."""
    payload = _payload(prompt="build the thing")
    handlers._handle_user_prompt_submit(payload)
    state = _state(payload)
    handlers._clear_turn(state)

    handlers._handle_stop(_payload(hook_event_name="Stop", last_assistant_message="done"))

    assert state_dir == []


# ---------------------------------------------------------------------------
# Todos
# ---------------------------------------------------------------------------


def test_only_the_post_write_todo_phase_is_counted(state_dir):
    """Todo hooks fire once per phase; counting both would double every item."""
    payload = _payload(prompt="plan")
    handlers._handle_user_prompt_submit(payload)

    for phase in ("validation", "postWrite"):
        handlers._handle_todo_created(_payload(hook_event_name="TodoCreated", phase=phase, todo_id="1"))
        handlers._handle_todo_completed(_payload(hook_event_name="TodoCompleted", phase=phase, todo_id="1"))

    state = _state(payload)
    assert state.get("todo_created_count") == "1"
    assert state.get("todo_completed_count") == "1"


def test_todo_events_without_a_phase_are_ignored(state_dir):
    handlers._handle_todo_created(_payload(hook_event_name="TodoCreated", todo_id="1"))

    assert _state(_payload()).get("todo_created_count") is None


# ---------------------------------------------------------------------------
# Subagents
# ---------------------------------------------------------------------------


def test_subagent_descriptor_is_recorded_between_start_and_stop(state_dir):
    payload = _payload(prompt="delegate")
    handlers._handle_user_prompt_submit(payload)

    handlers._handle_subagent_start(
        _payload(hook_event_name="SubagentStart", agent_id="general-purpose-call_1", agent_type="general-purpose")
    )
    handlers._handle_subagent_stop(
        _payload(
            hook_event_name="SubagentStop",
            agent_id="general-purpose-call_1",
            agent_type="general-purpose",
            last_assistant_message="42",
        )
    )

    pending = json.loads(_state(payload).get("pending_subagents") or "{}")
    entry = pending["general-purpose-call_1"]
    assert entry["agent_type"] == "general-purpose"
    assert entry["output"] == "42"
    assert entry["started_at_ms"] <= entry["ended_at_ms"]


def test_subagent_merge_does_not_reparse_the_parent_transcript(state_dir, tmp_path):
    """Regression: `agent_transcript_path` is the *parent* transcript, not a
    child file. Parsing it again duplicated the parent's calls under AGENT."""
    from core.event_model import EventStatus, TurnEvent

    parent = tmp_path / "chat.jsonl"
    parent.write_text(
        json.dumps(
            {
                "uuid": "a1",
                "sessionId": "sess-1",
                "type": "assistant",
                "timestamp": "2026-08-02T17:55:30.757Z",
                "model": "qwen3.5",
                "message": {"role": "model", "parts": [{"text": "parent output"}]},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    payload = _payload(prompt="delegate")
    handlers._handle_user_prompt_submit(payload)
    handlers._handle_subagent_start(_payload(hook_event_name="SubagentStart", agent_id="ag-1", agent_type="general"))
    handlers._handle_subagent_stop(
        _payload(
            hook_event_name="SubagentStop",
            agent_id="ag-1",
            agent_type="general",
            agent_transcript_path=str(parent),
            last_assistant_message="42",
        )
    )

    root = TurnEvent(
        event_id="turn-1",
        session_id="sess-1",
        turn_id="trace-1",
        sequence=0,
        started_at_ms=1,
        ended_at_ms=2,
        status=EventStatus.COMPLETED,
    )
    from core.event_model import EventGraph

    graph = EventGraph([root])
    handlers._merge_subagents(_state(payload), graph)

    kinds = [type(e).__name__ for e in graph.events]
    assert kinds.count("AgentEvent") == 1
    # The parent's model call must not reappear beneath the agent span.
    assert "ModelCallEvent" not in kinds


def test_two_subagents_bind_to_their_own_tool_calls(state_dir, tmp_path):
    """Regression: pairing by event order filed a subagent under the wrong call.

    Qwen embeds the invoking tool call id in `agent_id`, so binding uses that.
    Here the stop events arrive in the opposite order to the transcript calls —
    ordinal pairing would swap the two AGENT spans.
    """
    from core.event_model import EventGraph, EventStatus, ToolEvent, TurnEvent

    payload = _payload(prompt="delegate twice")
    handlers._handle_user_prompt_submit(payload)

    for agent_id in ("general-purpose-call_aaa", "general-purpose-call_bbb"):
        handlers._handle_subagent_start(
            _payload(hook_event_name="SubagentStart", agent_id=agent_id, agent_type="general-purpose")
        )
    # Stop events out of order relative to the transcript.
    for agent_id, answer in (("general-purpose-call_bbb", "second"), ("general-purpose-call_aaa", "first")):
        handlers._handle_subagent_stop(
            _payload(
                hook_event_name="SubagentStop",
                agent_id=agent_id,
                agent_type="general-purpose",
                last_assistant_message=answer,
            )
        )

    root = TurnEvent(
        event_id="turn-1",
        session_id="sess-1",
        turn_id="trace-1",
        sequence=0,
        started_at_ms=1,
        ended_at_ms=2,
        status=EventStatus.COMPLETED,
    )

    def _agent_tool(call_id, seq):
        return ToolEvent(
            event_id=f"tool-{call_id}",
            session_id="sess-1",
            turn_id="trace-1",
            parent_event_id="turn-1",
            sequence=seq,
            started_at_ms=1,
            ended_at_ms=2,
            status=EventStatus.COMPLETED,
            tool_call_id=call_id,
            tool_name="agent",
            source_id="general-purpose",
        )

    graph = EventGraph([root, _agent_tool("call_aaa", 1), _agent_tool("call_bbb", 2)])
    handlers._merge_subagents(_state(payload), graph)

    parents = {e.agent_id: e.parent_event_id for e in graph.events if type(e).__name__ == "AgentEvent"}
    assert parents["general-purpose-call_aaa"] == "tool-call_aaa"
    assert parents["general-purpose-call_bbb"] == "tool-call_bbb"


def test_unmatched_subagent_falls_back_to_the_turn(state_dir):
    """Evidence is kept rather than dropped when no tool call matches."""
    from core.event_model import EventGraph, EventStatus, TurnEvent

    payload = _payload(prompt="delegate")
    handlers._handle_user_prompt_submit(payload)
    handlers._handle_subagent_stop(
        _payload(hook_event_name="SubagentStop", agent_id="general-purpose-call_zzz", agent_type="general-purpose")
    )

    root = TurnEvent(
        event_id="turn-1",
        session_id="sess-1",
        turn_id="trace-1",
        sequence=0,
        started_at_ms=1,
        ended_at_ms=2,
        status=EventStatus.COMPLETED,
    )
    graph = EventGraph([root])
    handlers._merge_subagents(_state(payload), graph)

    agents = [e for e in graph.events if type(e).__name__ == "AgentEvent"]
    assert len(agents) == 1
    assert agents[0].parent_event_id == "turn-1"


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_entry_points_swallow_handler_failures(monkeypatch, capsys):
    """A hook must never take the host CLI down with it."""

    def boom(_payload):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(handlers, "_read_stdin", dict)
    handlers._entry("stop", boom)


def test_read_stdin_tolerates_garbage(monkeypatch):
    monkeypatch.setattr("sys.stdin", type("S", (), {"read": staticmethod(lambda: "not json")})())

    assert handlers._read_stdin() == {}


def test_read_stdin_decodes_utf8_regardless_of_locale(monkeypatch):
    """Hook payloads carry non-ASCII prompts; the locale codec must not apply.

    Reading text from stdin decodes with the interpreter locale, which on a
    Windows console set to a non-UTF-8 codepage raises UnicodeDecodeError and
    silently costs the turn its span (see upstream #88 for the same failure on
    transcript reads). Reading bytes keeps the payload intact everywhere.
    """
    import io

    payload = {"prompt": "привет 안녕 مرحبا", "session_id": "s1"}
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    class _Stdin:
        buffer = io.BytesIO(encoded)

        @staticmethod
        def read():  # pragma: no cover - must not be reached
            raise AssertionError("stdin must be read as bytes, not text")

    monkeypatch.setattr("sys.stdin", _Stdin())

    assert handlers._read_stdin() == payload
