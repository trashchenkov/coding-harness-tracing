"""Qwen Code hook handlers.

Several of these are regression tests for defects a live 0.21.3 session
surfaced; each names the behaviour it pins down.
"""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from tracing.qwen.hooks import adapter, handlers


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    """Point harness state at a temp directory and stop spans from being sent."""
    monkeypatch.setattr(handlers, "STATE_DIR", tmp_path, raising=False)
    monkeypatch.setattr("tracing.qwen.hooks.adapter.STATE_DIR", tmp_path)
    monkeypatch.setattr("tracing.qwen.hooks.adapter.PROJECTS_DIR", tmp_path)

    def test_transcript(payload, _session_id=None):
        raw = payload.get("transcript_path") or ""
        return adapter.validate_transcript_path(Path(raw), root=tmp_path) if raw else None

    def test_agent_transcript(payload, _session_id=None, _agent_id=None):
        raw = payload.get("agent_transcript_path") or ""
        return adapter.validate_transcript_path(Path(raw), root=tmp_path) if raw else None

    monkeypatch.setattr(handlers, "resolve_transcript_path", test_transcript)
    monkeypatch.setattr(handlers, "resolve_agent_transcript_path", test_agent_transcript)
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


def _assistant_line(text: str = "done", session_id: str = "sess-1") -> str:
    return json.dumps(
        {
            "uuid": "assistant-1",
            "sessionId": session_id,
            "type": "assistant",
            "timestamp": "2026-08-02T17:55:30.757Z",
            "model": "qwen3.5",
            "message": {"role": "model", "parts": [{"text": text}]},
        }
    )


def _first_span(payload: dict) -> dict:
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


# ---------------------------------------------------------------------------
# Transcript path confinement
# ---------------------------------------------------------------------------


def test_root_transcript_is_bound_to_session_and_cwd(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    chats = projects / "-work" / "chats"
    chats.mkdir(parents=True)
    victim = chats / "victim.jsonl"
    victim.write_text(_assistant_line("victim") + "\n", encoding="utf-8")
    attacker = chats / "attacker.jsonl"
    attacker.write_text(_assistant_line("attacker") + "\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "PROJECTS_DIR", projects)

    payload = {"cwd": "/work", "session_id": "attacker", "transcript_path": str(victim)}
    assert adapter.resolve_transcript_path(payload) is None

    payload["transcript_path"] = str(attacker)
    assert adapter.resolve_transcript_path(payload) == attacker.resolve()


def test_child_transcript_is_bound_to_parent_session_and_agent(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    child_root = projects / "-work" / "subagents" / "parent"
    child_root.mkdir(parents=True)
    expected = child_root / "agent-agent-1.jsonl"
    expected.write_text(_assistant_line("expected") + "\n", encoding="utf-8")
    unrelated = child_root / "agent-agent-2.jsonl"
    unrelated.write_text(_assistant_line("unrelated") + "\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "PROJECTS_DIR", projects)

    payload = {
        "cwd": "/work",
        "session_id": "parent",
        "agent_id": "agent-1",
        "agent_transcript_path": str(unrelated),
    }
    assert adapter.resolve_agent_transcript_path(payload) is None

    payload["agent_transcript_path"] = str(expected)
    assert adapter.resolve_agent_transcript_path(payload) == expected.resolve()


def test_child_transcript_rejects_noncanonical_colliding_agent_ids(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    child_root = projects / "-work" / "subagents" / "parent"
    child_root.mkdir(parents=True)
    colliding = child_root / "agent-agent_1.jsonl"
    colliding.write_text(_assistant_line("foreign") + "\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "PROJECTS_DIR", projects)

    for agent_id in ("agent:1", "agent?1"):
        payload = {
            "cwd": "/work",
            "session_id": "parent",
            "agent_id": agent_id,
            "agent_transcript_path": str(colliding),
        }
        assert adapter.resolve_agent_transcript_path(payload) is None


def test_transcript_swap_to_symlink_is_rejected_after_validation(tmp_path):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text(_assistant_line("safe") + "\n", encoding="utf-8")
    validated = adapter.validate_transcript_path(transcript, root=tmp_path)
    assert validated is not None
    outside = tmp_path.parent / f"{tmp_path.name}-secret.jsonl"
    outside.write_text(_assistant_line("secret") + "\n", encoding="utf-8")
    transcript.unlink()
    transcript.symlink_to(outside)

    assert handlers._transcript_snapshot(validated, 0) is None


def test_direct_transcript_path_outside_projects_is_rejected(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    projects.mkdir()
    outside = tmp_path / "secret.jsonl"
    outside.write_text(_assistant_line("secret") + "\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "PROJECTS_DIR", projects)

    assert adapter.resolve_transcript_path({"transcript_path": str(outside)}) is None


def test_fallback_session_id_cannot_traverse_projects(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    (projects / "work" / "chats").mkdir(parents=True)
    escaped = projects / "secret.jsonl"
    escaped.write_text(_assistant_line("secret") + "\n", encoding="utf-8")
    monkeypatch.setattr(adapter, "PROJECTS_DIR", projects)

    payload = {"cwd": "work", "session_id": "../../secret"}
    assert adapter.resolve_transcript_path(payload) is None


def test_transcript_symlink_cannot_escape_projects(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    chats = projects / "work" / "chats"
    chats.mkdir(parents=True)
    outside = tmp_path / "secret.jsonl"
    outside.write_text(_assistant_line("secret") + "\n", encoding="utf-8")
    link = chats / "sess.jsonl"
    link.symlink_to(outside)
    monkeypatch.setattr(adapter, "PROJECTS_DIR", projects)

    assert adapter.resolve_transcript_path({"transcript_path": str(link)}) is None


# ---------------------------------------------------------------------------
# Turn boundaries
# ---------------------------------------------------------------------------


def test_prompted_event_opens_a_turn(state_dir):
    payload = _payload(prompt="build the thing")

    handlers._handle_user_prompt_submit(payload)

    assert _state(payload).get("current_trace_id")


def test_disabled_tracing_entry_does_not_read_or_collect(monkeypatch):
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
    monkeypatch.setattr(handlers, "_read_stdin", lambda: pytest.fail("stdin should not be read"))

    handlers._entry("probe", lambda _payload: pytest.fail("handler should not run"))


def test_privacy_opt_out_redacts_durable_prompt_and_subagent_state(state_dir, tmp_path, monkeypatch):
    for name in ("ARIZE_LOG_PROMPTS", "ARIZE_LOG_TOOL_DETAILS", "ARIZE_LOG_TOOL_CONTENT"):
        monkeypatch.setenv(name, "false")
    payload = _payload(prompt="USER-SECRET")
    handlers._handle_user_prompt_submit(payload)
    handlers._handle_subagent_start(_payload(agent_id="general-purpose-call_1", prompt="AGENT-SECRET"))
    handlers._handle_subagent_stop(
        _payload(
            hook_event_name="SubagentStop",
            agent_id="general-purpose-call_1",
            agent_transcript_path="/tmp/TOKEN_supersecret_123.jsonl",
            last_assistant_message="yes",
        )
    )

    root_transcript = tmp_path / "privacy-root.jsonl"
    root_transcript.write_text("", encoding="utf-8")
    root_payload = _payload(
        session_id="privacy-root",
        prompt="ROOT-SECRET",
        transcript_path=str(root_transcript),
    )
    handlers._handle_user_prompt_submit(root_payload)
    root_transcript.write_text(_assistant_line("yes", "privacy-root") + "\n", encoding="utf-8")
    handlers._handle_stop(
        _payload(
            session_id="privacy-root",
            hook_event_name="Stop",
            transcript_path=str(root_transcript),
            last_assistant_message="yes",
        )
    )

    state = _state(payload)
    persisted = "\n".join(path.read_text(encoding="utf-8") for path in tmp_path.glob("*.json"))
    assert state.state_file is not None
    assert "USER-SECRET" not in persisted
    assert "ROOT-SECRET" not in persisted
    assert "AGENT-SECRET" not in persisted
    assert '"yes"' not in persisted
    assert "TOKEN_supersecret_123" not in persisted
    assert "8a798890fe93817163b10b5f7bd2ca4d25d84c52739a645a889c173eee7d9d3d" not in persisted
    assert "transcript_sha256" not in persisted
    assert "<redacted" in persisted


def test_privacy_opt_out_is_frozen_for_the_turn(state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "privacy.jsonl"
    transcript.write_text("", encoding="utf-8")
    for name in ("ARIZE_LOG_PROMPTS", "ARIZE_LOG_TOOL_DETAILS", "ARIZE_LOG_TOOL_CONTENT"):
        monkeypatch.setenv(name, "false")
    payload = _payload(prompt="USER-SECRET", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)

    records = [
        {
            "uuid": "u1",
            "sessionId": "sess-1",
            "type": "user",
            "timestamp": "2026-08-02T17:55:29.000Z",
            "message": {"role": "user", "parts": [{"text": "PROMPT-SECRET"}]},
        },
        {
            "uuid": "a1",
            "parentUuid": "u1",
            "sessionId": "sess-1",
            "type": "assistant",
            "timestamp": "2026-08-02T17:55:30.000Z",
            "message": {
                "role": "model",
                "parts": [
                    {"text": "MODEL-SECRET"},
                    {"functionCall": {"id": "call-1", "name": "read_file", "args": {"path": "ARG-SECRET"}}},
                ],
            },
        },
        {
            "uuid": "r1",
            "parentUuid": "a1",
            "sessionId": "sess-1",
            "type": "tool_result",
            "timestamp": "2026-08-02T17:55:31.000Z",
            "message": {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "id": "call-1",
                            "name": "read_file",
                            "response": {"output": "RESULT-SECRET"},
                        }
                    }
                ],
            },
        },
    ]
    transcript.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    for name in ("ARIZE_LOG_PROMPTS", "ARIZE_LOG_TOOL_DETAILS", "ARIZE_LOG_TOOL_CONTENT"):
        monkeypatch.setenv(name, "true")

    handlers._handle_stop(
        _payload(
            hook_event_name="Stop",
            transcript_path=str(transcript),
            last_assistant_message="MODEL-SECRET",
        )
    )

    exported = json.dumps(state_dir)
    for secret in ("USER-SECRET", "PROMPT-SECRET", "MODEL-SECRET", "ARG-SECRET", "RESULT-SECRET"):
        assert secret not in exported
    assert "<redacted" in exported


def test_blocking_stop_continuation_updates_same_turn(state_dir, tmp_path):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    state = _state(payload)
    trace_id = state.get("current_trace_id")
    span_id = state.get("current_trace_span_id")

    transcript.write_text(_assistant_line("first answer") + "\n", encoding="utf-8")
    handlers._handle_stop(
        _payload(
            hook_event_name="Stop",
            transcript_path=str(transcript),
            stop_hook_active=True,
            last_assistant_message="first answer",
        )
    )

    state = _state(payload)
    assert len(state_dir) == 1
    assert state.get("current_trace_id") == trace_id
    assert state.get("turn_exported") == "1"

    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_assistant_line("continued final answer") + "\n")
    handlers._handle_stop(
        _payload(
            hook_event_name="Stop",
            transcript_path=str(transcript),
            stop_hook_active=True,
            last_assistant_message="continued final answer",
        )
    )

    state = _state(payload)
    assert len(state_dir) == 2
    assert state.get("current_trace_id") == trace_id
    assert state.get("current_trace_span_id") == span_id
    assert state.get("trace_count") == "1"
    first_root = _first_span(state_dir[0])
    second_root = _first_span(state_dir[1])
    assert first_root["traceId"] == second_root["traceId"] == trace_id
    assert first_root["spanId"] == second_root["spanId"] == span_id
    assert "continued final answer" in json.dumps(state_dir[1])

    handlers._handle_session_end(_payload(hook_event_name="SessionEnd", transcript_path=str(transcript)))
    closed = _state(payload)
    assert closed.get("current_trace_id") is None
    assert closed.get("session_closed") == "1"


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


def test_noninteractive_prompt_projection_does_not_split_active_turn(state_dir):
    """Only submitted_prompt proves a new interactive user submission.

    Qwen can re-fire UserPromptSubmit for a machine continuation with a non-empty
    model-bound prompt but without submitted_prompt.
    """
    payload = _payload(prompt="build the thing")
    handlers._handle_user_prompt_submit(payload)
    first = _state(payload).get("current_trace_id")

    handlers._handle_user_prompt_submit(_payload(prompt="tool result projection"))

    state = _state(payload)
    assert state.get("current_trace_id") == first
    assert state.get("trace_count") == "1"


def test_submitted_prompt_is_the_user_visible_turn_input(state_dir):
    payload = _payload(prompt="expanded model-bound context", submitted_prompt="ship it")

    handlers._handle_user_prompt_submit(payload)

    assert _state(payload).get("current_trace_prompt") == "ship it"


def test_cleared_turn_is_not_exported_again(state_dir):
    """Regression: turn keys were blanked rather than deleted, so a cleared
    turn passed an `is None` check and re-exported with an empty trace id."""
    payload = _payload(prompt="build the thing")
    handlers._handle_user_prompt_submit(payload)
    state = _state(payload)
    handlers._clear_turn(state)

    handlers._handle_stop(_payload(hook_event_name="Stop", last_assistant_message="done"))

    assert state_dir == []


def test_foreign_session_assistant_is_not_an_authoritative_terminal_marker():
    snapshot = _assistant_line("done", session_id="foreign-session") + "\n"

    assert not handlers._tail_has_assistant_message(
        snapshot,
        0,
        "done",
        expected_session_id="session-1",
    )


def test_stop_without_transcript_retains_turn_for_retry(state_dir):
    payload = _payload(prompt="build the thing")
    handlers._handle_user_prompt_submit(payload)
    trace_id = _state(payload).get("current_trace_id")

    handlers._handle_stop(_payload(hook_event_name="Stop", last_assistant_message="done"))

    assert state_dir == []
    assert _state(payload).get("current_trace_id") == trace_id


def test_session_end_exports_completed_turn_when_stop_did_not_fire(state_dir, tmp_path):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build the thing", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text(_assistant_line("durable final answer") + "\n", encoding="utf-8")

    handlers._handle_session_end(_payload(hook_event_name="SessionEnd", reason="exit", transcript_path=str(transcript)))

    assert len(state_dir) == 1
    closed = _state(payload)
    assert closed.get("current_trace_id") is None
    assert closed.get("session_closed") == "1"
    assert "durable final answer" in json.dumps(state_dir[0])


def test_session_end_retains_turn_when_final_export_fails(state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build the thing", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text(_assistant_line() + "\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "send_span", lambda _payload: False)
    state = _state(payload)
    trace_id = state.get("current_trace_id")
    state_file = state.state_file
    lock_path = state._lock_path

    handlers._handle_session_end(_payload(hook_event_name="SessionEnd", reason="exit", transcript_path=str(transcript)))

    assert state_dir == []
    assert _state(payload).get("current_trace_id") == trace_id
    assert state_file is not None and state_file.exists()
    assert lock_path is not None and lock_path.exists()


def test_successful_transport_is_not_duplicated_when_final_marker_write_fails(state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text(_assistant_line("done") + "\n", encoding="utf-8")
    state = _state(payload)
    state_type = type(state)
    original_set = state_type.set
    sends = []

    def fail_final_marker(self, key, value):
        if key == "turn_exported":
            return False
        return original_set(self, key, value)

    monkeypatch.setattr(state_type, "set", fail_final_marker)
    monkeypatch.setattr(handlers, "send_span", lambda span: sends.append(span) or True)
    stop = _payload(
        hook_event_name="Stop",
        transcript_path=str(transcript),
        last_assistant_message="done",
    )

    handlers._handle_stop(stop)
    handlers._handle_stop(stop)

    assert len(sends) == 1


def test_stop_failure_without_matching_terminal_marker_retains_turn(state_dir, tmp_path):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    trace_id = _state(payload).get("current_trace_id")
    transcript.write_text(_assistant_line("done") + "\n", encoding="utf-8")

    handlers._handle_stop_failure(
        _payload(
            hook_event_name="StopFailure",
            transcript_path=str(transcript),
            last_assistant_message="failed",
        )
    )

    assert state_dir == []
    assert _state(payload).get("current_trace_id") == trace_id


def test_stop_failure_with_matching_terminal_marker_exports_error(state_dir, tmp_path):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text(_assistant_line("failed") + "\n", encoding="utf-8")

    handlers._handle_stop_failure(
        _payload(
            hook_event_name="StopFailure",
            transcript_path=str(transcript),
            last_assistant_message="failed",
        )
    )

    assert _first_span(state_dir[0])["status"]["code"] == 2


def test_complete_transcript_without_authoritative_marker_retains_turn(state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    trace_id = _state(payload).get("current_trace_id")
    transcript.write_text(_assistant_line("arbitrary earlier response") + "\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "TRANSCRIPT_STABILITY_RETRIES", 0, raising=False)

    handlers._handle_stop(_payload(hook_event_name="Stop", transcript_path=str(transcript)))

    assert state_dir == []
    assert _state(payload).get("current_trace_id") == trace_id


def test_persistent_malformed_transcript_tail_is_retained(state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text('{"type":"assistant"', encoding="utf-8")
    monkeypatch.setattr(handlers, "TRANSCRIPT_STABILITY_RETRIES", 1, raising=False)
    monkeypatch.setattr(handlers, "TRANSCRIPT_STABILITY_DELAY_SECONDS", 0, raising=False)

    handlers._handle_stop(_payload(hook_event_name="Stop", transcript_path=str(transcript)))

    assert state_dir == []
    assert _state(payload).get("current_trace_id")


def test_transient_malformed_transcript_tail_is_retried(state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text('{"type":"assistant"', encoding="utf-8")
    retries = []

    def finish_write(_delay):
        retries.append(1)
        if len(retries) == 1:
            transcript.write_text(_assistant_line() + "\n", encoding="utf-8")

    monkeypatch.setattr(handlers, "TRANSCRIPT_STABILITY_RETRIES", 2, raising=False)
    monkeypatch.setattr(handlers, "TRANSCRIPT_STABILITY_DELAY_SECONDS", 0, raising=False)
    monkeypatch.setattr(handlers, "time", type("Clock", (), {"sleep": staticmethod(finish_write)})(), raising=False)

    handlers._handle_stop(
        _payload(hook_event_name="Stop", transcript_path=str(transcript), last_assistant_message="done")
    )

    assert retries == [1]
    assert len(state_dir) == 1
    assert _state(payload).get("turn_exported") == "1"


def test_complete_transcript_tail_must_remain_unchanged_before_export(tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text(_assistant_line("first") + "\n", encoding="utf-8")
    observations = []

    def append_complete_line(_delay):
        observations.append(1)
        if len(observations) == 1:
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(_assistant_line("late") + "\n")

    monkeypatch.setattr(handlers, "TRANSCRIPT_STABILITY_RETRIES", 2, raising=False)
    monkeypatch.setattr(handlers, "TRANSCRIPT_STABILITY_DELAY_SECONDS", 0, raising=False)
    monkeypatch.setattr(handlers, "time", type("Clock", (), {"sleep": staticmethod(append_complete_line)})())

    snapshot = handlers._wait_for_stable_transcript(transcript, 0, "late")
    assert observations == [1]
    assert snapshot == transcript.read_text(encoding="utf-8")
    assert "late" in snapshot


def test_stop_waits_for_delayed_authoritative_assistant_record(state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text(_assistant_line("earlier") + "\n", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    appended = threading.Event()

    def delayed_append():
        time.sleep(0.06)
        with transcript.open("a", encoding="utf-8") as handle:
            handle.write(_assistant_line("final") + "\n")
        appended.set()

    def verify_send(span):
        assert appended.is_set()
        state_dir.append(span)
        return True

    monkeypatch.setattr(handlers, "send_span", verify_send)
    writer = threading.Thread(target=delayed_append)
    writer.start()
    handlers._handle_stop(
        _payload(hook_event_name="Stop", transcript_path=str(transcript), last_assistant_message="final")
    )
    writer.join(timeout=2)

    assert not writer.is_alive()
    assert len(state_dir) == 1
    assert _state(payload).get("turn_exported") == "1"


def test_prompt_offset_does_not_skip_unterminated_record(state_dir, tmp_path):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text(_assistant_line("in-flight"), encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))

    handlers._handle_user_prompt_submit(payload)

    assert _state(payload).get("trace_start_line") == "0"


def test_concurrent_stops_export_turn_once(state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text(_assistant_line() + "\n", encoding="utf-8")

    active = 0
    maximum = 0
    calls = 0
    counter_lock = threading.Lock()

    def slow_send(_payload):
        nonlocal active, maximum, calls
        with counter_lock:
            active += 1
            calls += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with counter_lock:
            active -= 1
        return True

    monkeypatch.setattr(handlers, "send_span", slow_send)
    start = threading.Barrier(2)

    def stop_once():
        start.wait()
        handlers._handle_stop(
            _payload(hook_event_name="Stop", transcript_path=str(transcript), last_assistant_message="done")
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _index: stop_once(), range(2)))

    assert calls == 1
    assert maximum == 1


def test_session_end_replaces_state_with_bounded_tombstone_and_keeps_file_lock(state_dir):
    payload = _payload(hook_event_name="SessionStart", source="startup")
    handlers._handle_session_start(payload)
    state = handlers.resolve_session(payload)
    state_file = state.state_file
    lock_path = state._lock_path
    assert state_file is not None and state_file.exists()
    assert lock_path is not None and lock_path.exists()
    inode = lock_path.stat().st_ino

    handlers._handle_session_end(_payload(hook_event_name="SessionEnd", reason="exit"))

    assert state_file.exists()
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"session_closed": "1"}
    assert lock_path.exists()
    assert lock_path.stat().st_ino == inode


def test_delayed_prompt_cannot_resurrect_closed_session(state_dir, monkeypatch):
    session = _payload(hook_event_name="SessionStart", source="startup")
    handlers._handle_session_start(session)
    original_resolve = handlers.resolve_session
    resolved = threading.Event()
    release = threading.Event()

    def pause_after_resolve(payload):
        state = original_resolve(payload)
        if payload.get("hook_event_name") == "UserPromptSubmit":
            resolved.set()
            assert release.wait(timeout=2)
        return state

    monkeypatch.setattr(handlers, "resolve_session", pause_after_resolve)
    delayed = threading.Thread(
        target=handlers._handle_user_prompt_submit,
        args=(_payload(prompt="late secret"),),
    )
    delayed.start()
    assert resolved.wait(timeout=2)
    handlers._handle_session_end(_payload(hook_event_name="SessionEnd", reason="exit"))
    release.set()
    delayed.join(timeout=2)

    assert not delayed.is_alive()
    state = original_resolve(session)
    assert state.get("session_closed") == "1"
    assert state.get("current_trace_id") is None
    assert "late secret" not in (state.state_file or pytest.fail()).read_text(encoding="utf-8")


def test_delayed_subagent_events_cannot_recreate_cleared_turn(state_dir, tmp_path):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="delegate", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text(_assistant_line("done") + "\n", encoding="utf-8")
    handlers._handle_stop(
        _payload(hook_event_name="Stop", transcript_path=str(transcript), last_assistant_message="done")
    )

    handlers._handle_session_end(_payload(hook_event_name="SessionEnd", transcript_path=str(transcript), reason="exit"))

    handlers._handle_subagent_start(_payload(hook_event_name="SubagentStart", agent_id="late-agent"))
    handlers._handle_subagent_stop(
        _payload(hook_event_name="SubagentStop", agent_id="late-agent", last_assistant_message="late")
    )

    state = _state(payload)
    assert state.get("current_trace_id") is None
    assert state.get("pending_subagents") is None


def test_lock_files_use_bounded_deterministic_shards(state_dir, monkeypatch):
    monkeypatch.setattr(adapter, "LOCK_SHARD_COUNT", 1)

    first = adapter.resolve_session(_payload(session_id="first"))
    second = adapter.resolve_session(_payload(session_id="second"))

    assert first.state_file != second.state_file
    assert first._lock_path == second._lock_path == adapter.STATE_DIR / ".lock_shard_000"
    assert (
        handlers._operation_lock(first, "lifecycle").lock_path
        == handlers._operation_lock(second, "lifecycle").lock_path
    )


def test_resolve_session_has_no_initialization_side_effect(state_dir):
    state = adapter.resolve_session(_payload(session_id="shared-session"))

    assert state.state_file is not None
    assert not state.state_file.exists()


def test_explicit_numeric_or_pathlike_session_id_is_hashed_and_not_pid_gc(state_dir, monkeypatch):
    payload = _payload(session_id="../../12345")
    state = adapter.resolve_session(payload)
    state.set("marker", "keep")
    assert state.state_file is not None
    assert state.state_file.parent == adapter.STATE_DIR
    assert state.state_file.name.startswith("state_session_")
    assert "12345" not in state.state_file.name

    monkeypatch.setattr(adapter, "_is_pid_alive", lambda _pid: False)
    adapter.gc_stale_state_files()

    assert state.state_file.exists()


def test_gc_removes_only_expired_explicit_session_state(state_dir):
    expired = adapter.STATE_DIR / "state_session_expired.json"
    fresh = adapter.STATE_DIR / "state_session_fresh.json"
    expired.write_text("{}", encoding="utf-8")
    fresh.write_text("{}", encoding="utf-8")
    old = time.time() - adapter.EXPLICIT_STATE_TTL_SECONDS - 1
    os.utime(expired, (old, old))

    adapter.gc_stale_state_files()

    assert not expired.exists()
    assert fresh.exists()


def test_gc_does_not_delete_expired_state_with_live_lifecycle_owner(state_dir):
    payload = _payload(session_id="active-old-session")
    state = adapter.resolve_session(payload)
    assert state.state_file is not None
    with handlers._operation_lock(state, "lifecycle"):
        pass
    old = time.time() - adapter.EXPLICIT_STATE_TTL_SECONDS - 1
    os.utime(state.state_file, (old, old))

    with handlers._operation_lock(state, "lifecycle"):
        adapter.gc_stale_state_files()
        assert state.state_file.exists()

    adapter.gc_stale_state_files()
    assert not state.state_file.exists()


def test_session_end_waits_for_active_stop_without_unlinking_its_lock(state_dir, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="ship it", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text(_assistant_line() + "\n", encoding="utf-8")
    lock_path = handlers._operation_lock(_state(payload), "lifecycle").lock_path

    sender_entered = threading.Event()
    release_sender = threading.Event()

    def blocking_send(span):
        sender_entered.set()
        assert release_sender.wait(timeout=2)
        state_dir.append(span)
        return True

    monkeypatch.setattr(handlers, "send_span", blocking_send)
    stop_payload = _payload(hook_event_name="Stop", transcript_path=str(transcript), last_assistant_message="done")
    stop_thread = threading.Thread(target=handlers._handle_stop, args=(stop_payload,))
    stop_thread.start()
    assert sender_entered.wait(timeout=2)
    inode = lock_path.stat().st_ino

    end_done = threading.Event()

    def end_session():
        handlers._handle_session_end(_payload(hook_event_name="SessionEnd", reason="exit"))
        end_done.set()

    end_thread = threading.Thread(target=end_session)
    end_thread.start()
    assert not end_done.wait(timeout=0.05)
    assert lock_path.exists() and lock_path.stat().st_ino == inode

    release_sender.set()
    stop_thread.join(timeout=2)
    end_thread.join(timeout=2)
    assert not stop_thread.is_alive() and not end_thread.is_alive()
    assert lock_path.exists() and lock_path.stat().st_ino == inode
    assert _state(payload).get("current_trace_id") is None


def test_prompt_initialization_is_serialized_with_session_end(state_dir, monkeypatch):
    payload = _payload(prompt="race")
    handlers._handle_session_start(payload)
    entered = threading.Event()
    release = threading.Event()
    end_done = threading.Event()
    original = handlers.ensure_session_initialized

    def pausing_initialize(state, event):
        original(state, event)
        if threading.current_thread().name == "prompt-race":
            entered.set()
            assert release.wait(timeout=2)

    monkeypatch.setattr(handlers, "ensure_session_initialized", pausing_initialize)
    prompt_thread = threading.Thread(
        target=handlers._handle_user_prompt_submit,
        args=(payload,),
        name="prompt-race",
    )
    prompt_thread.start()
    assert entered.wait(timeout=2)

    def end_session():
        handlers._handle_session_end(_payload(hook_event_name="SessionEnd", reason="exit"))
        end_done.set()

    end_thread = threading.Thread(target=end_session)
    end_thread.start()
    ended_before_prompt_initialization_completed = end_done.wait(timeout=0.05)
    release.set()
    prompt_thread.join(timeout=2)
    end_thread.join(timeout=2)

    assert not ended_before_prompt_initialization_completed
    assert not prompt_thread.is_alive() and not end_thread.is_alive()
    assert _state(payload).get("session_id") == payload["session_id"]
    assert _state(payload).get("current_trace_id")


def test_running_subagent_blocks_export_until_its_state_is_complete(state_dir, tmp_path, monkeypatch):
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="delegate", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    handlers._handle_subagent_start(
        _payload(
            hook_event_name="SubagentStart",
            agent_id="general-call_1",
            agent_type="general",
            prompt="inspect the logs",
        )
    )
    transcript.write_text(_assistant_line() + "\n", encoding="utf-8")

    handlers._handle_stop(_payload(hook_event_name="Stop", transcript_path=str(transcript)))

    assert state_dir == []
    assert _state(payload).get("current_trace_id")

    handlers._handle_subagent_stop(
        _payload(
            hook_event_name="SubagentStop",
            agent_id="general-call_1",
            agent_type="general",
            last_assistant_message="done",
        )
    )
    handlers._handle_stop(
        _payload(hook_event_name="Stop", transcript_path=str(transcript), last_assistant_message="done")
    )

    spans = state_dir[0]["resourceSpans"][0]["scopeSpans"][0]["spans"]
    agent = next(span for span in spans if span["name"].startswith("Subagent:"))
    attrs = {item["key"]: next(iter(item["value"].values())) for item in agent["attributes"]}
    assert attrs["input.value"] == "inspect the logs"
    assert _state(payload).get("turn_exported") == "1"


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


def test_concurrent_subagent_starts_do_not_lose_descriptors(state_dir, monkeypatch):
    payload = _payload(prompt="delegate widely")
    handlers._handle_user_prompt_submit(payload)

    def slow_clock():
        time.sleep(0.01)
        return 100

    monkeypatch.setattr(handlers, "get_timestamp_ms", slow_clock)

    def start(index):
        handlers._handle_subagent_start(
            _payload(hook_event_name="SubagentStart", agent_id=f"agent-{index}", agent_type="general")
        )

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(start, range(16)))

    pending = json.loads(_state(payload).get("pending_subagents") or "{}")
    assert set(pending) == {f"agent-{index}" for index in range(16)}


def test_started_subagent_without_stop_is_running_not_completed(state_dir):
    from core.event_model import EventGraph, EventStatus, TurnEvent

    payload = _payload(prompt="delegate")
    handlers._handle_user_prompt_submit(payload)
    handlers._handle_subagent_start(_payload(hook_event_name="SubagentStart", agent_id="ag-1", agent_type="general"))
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

    handlers._merge_subagents(_state(payload), graph, payload)

    agent = next(event for event in graph.events if type(event).__name__ == "AgentEvent")
    assert agent.status == EventStatus.RUNNING
    assert agent.ended_at_ms is None


def test_background_subagent_child_transcript_is_nested(state_dir, tmp_path):
    from core.event_model import EventGraph, EventStatus, TurnEvent

    parent = tmp_path / "parent.jsonl"
    parent.write_text("", encoding="utf-8")
    child = tmp_path / "child.jsonl"
    child.write_text(_assistant_line("child answer") + "\n", encoding="utf-8")
    payload = _payload(prompt="delegate")
    handlers._handle_user_prompt_submit(payload)
    handlers._handle_subagent_start(_payload(hook_event_name="SubagentStart", agent_id="ag-1", agent_type="general"))
    handlers._handle_subagent_stop(
        _payload(
            hook_event_name="SubagentStop",
            agent_id="ag-1",
            agent_type="general",
            agent_transcript_path=str(child),
            last_assistant_message="child answer",
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
    graph = EventGraph([root])

    handlers._merge_subagents(_state(payload), graph, payload, parent_transcript=parent)

    agent = next(event for event in graph.events if type(event).__name__ == "AgentEvent")
    model = next(event for event in graph.events if type(event).__name__ == "ModelCallEvent")
    assert model.parent_event_id == agent.event_id
    assert model.output == "child answer"


def test_missing_declared_child_transcript_retains_root_turn(state_dir, tmp_path, monkeypatch):
    parent = tmp_path / "parent.jsonl"
    parent.write_text("", encoding="utf-8")
    missing_child = tmp_path / "missing-child.jsonl"
    payload = _payload(prompt="delegate", transcript_path=str(parent))
    handlers._handle_user_prompt_submit(payload)
    handlers._handle_subagent_start(_payload(hook_event_name="SubagentStart", agent_id="ag-1", agent_type="general"))
    handlers._handle_subagent_stop(
        _payload(
            hook_event_name="SubagentStop",
            agent_id="ag-1",
            agent_type="general",
            agent_transcript_path=str(missing_child),
            last_assistant_message="child answer",
        )
    )
    parent.write_text(_assistant_line("done") + "\n", encoding="utf-8")
    monkeypatch.setattr(handlers, "TRANSCRIPT_STABILITY_RETRIES", 0)

    handlers._handle_stop(
        _payload(
            hook_event_name="Stop",
            transcript_path=str(parent),
            last_assistant_message="done",
        )
    )

    assert state_dir == []
    assert _state(payload).get("current_trace_id")


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
    handlers._merge_subagents(_state(payload), graph, payload)

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
    handlers._merge_subagents(_state(payload), graph, payload)

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
    handlers._merge_subagents(_state(payload), graph, payload)

    agents = [e for e in graph.events if type(e).__name__ == "AgentEvent"]
    assert len(agents) == 1
    assert agents[0].parent_event_id == "turn-1"


# ---------------------------------------------------------------------------
# Robustness
# ---------------------------------------------------------------------------


def test_stop_entry_points_return_normally(monkeypatch):
    monkeypatch.setattr(handlers, "_read_stdin", dict)

    assert handlers.stop() is None
    assert handlers.subagent_stop() is None


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
