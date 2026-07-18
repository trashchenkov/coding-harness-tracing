"""Cursor 2.5+ hook contract regressions grounded in official docs and CLI bundle."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from tracing.cursor.hooks import adapter
from tracing.cursor.hooks.handlers import _dispatch


@pytest.fixture(autouse=True)
def _state_dir(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
    return state_dir


@pytest.fixture
def captured_spans():
    sent = []
    with mock.patch(
        "tracing.cursor.hooks.handlers._send_span_to_backend",
        side_effect=lambda payload: sent.append(payload) or True,
    ):
        yield sent


def _span(payload):
    return payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]


def _attrs(span):
    return {item["key"]: next(iter(item["value"].values())) for item in span["attributes"]}


def test_dispatch_supports_all_new_events(monkeypatch):
    handlers = {
        "preToolUse": "_handle_pre_tool_use",
        "postToolUseFailure": "_handle_post_tool_use_failure",
        "subagentStart": "_handle_subagent_start",
        "subagentStop": "_handle_subagent_stop",
        "preCompact": "_handle_pre_compact",
        "workspaceOpen": "_handle_workspace_open",
    }
    for event, function_name in handlers.items():
        with mock.patch(f"tracing.cursor.hooks.handlers.{function_name}") as handler:
            _dispatch(event, {"conversation_id": "c", "generation_id": "g"})
            handler.assert_called_once()


def test_generic_tool_pairs_by_tool_use_id_and_serializes_json(captured_spans):
    common = {"conversation_id": "c", "generation_id": "g", "tool_name": "Grep"}
    with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
        _dispatch("preToolUse", {**common, "tool_use_id": "tool-a", "tool_input": {"query": "alpha"}})
        _dispatch("preToolUse", {**common, "tool_use_id": "tool-b", "tool_input": {"query": "beta"}})
    with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1300):
        _dispatch("postToolUse", {**common, "tool_use_id": "tool-a", "tool_output": "A", "duration": 300})
    with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1500):
        _dispatch("postToolUse", {**common, "tool_use_id": "tool-b", "tool_output": "B", "duration": 500})

    assert len(captured_spans) == 2
    first, second = map(_span, captured_spans)
    assert _attrs(first)["input.value"] == json.dumps({"query": "alpha"}, separators=(",", ":"))
    assert _attrs(second)["input.value"] == json.dumps({"query": "beta"}, separators=(",", ":"))
    assert _attrs(first)["output.value"] == "A"
    assert _attrs(second)["output.value"] == "B"
    assert first["startTimeUnixNano"].startswith("1000")
    assert second["startTimeUnixNano"].startswith("1000")


def test_tool_failure_emits_error_span_and_cleans_pair(captured_spans, _state_dir):
    common = {
        "conversation_id": "c",
        "generation_id": "g",
        "tool_name": "Shell",
        "tool_use_id": "failed-tool",
        "tool_input": {"command": "false"},
    }
    with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
        _dispatch("preToolUse", common)
    with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1200):
        _dispatch(
            "postToolUseFailure",
            {**common, "error_message": "exit 1", "failure_type": "error", "duration": 200, "is_interrupt": False},
        )

    span = _span(captured_spans[0])
    attrs = _attrs(span)
    assert attrs["tool.name"] == "Shell"
    assert attrs["cursor.tool.status"] == "error"
    assert attrs["cursor.tool.failure_type"] == "error"
    assert attrs["cursor.tool.is_interrupt"] is False
    assert not list(_state_dir.glob("*failed-tool*.stack.json"))


def test_subagents_pair_from_declared_shared_fields(captured_spans, _state_dir):
    common = {"conversation_id": "c", "generation_id": "g", "subagent_type": "explore"}
    with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
        _dispatch("subagentStart", {**common, "subagent_id": "a", "task": "first"})
        _dispatch("subagentStart", {**common, "subagent_id": "b", "task": "second"})
    with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1400):
        _dispatch("subagentStop", {**common, "task": "first", "summary": "A", "status": "completed"})
    with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1500):
        _dispatch("subagentStop", {**common, "task": "second", "summary": "B", "status": "completed"})

    spans = [_span(payload) for payload in captured_spans]
    assert [_attrs(span)["input.value"] for span in spans] == ["first", "second"]
    assert [_attrs(span)["output.value"] for span in spans] == ["A", "B"]
    assert [_attrs(span)["cursor.subagent.id"] for span in spans] == ["a", "b"]
    assert all(span["startTimeUnixNano"].startswith("1000") for span in spans)
    assert not list(_state_dir.glob("subagent_*.stack.json"))


@pytest.mark.parametrize(
    ("log_prompts", "log_model_outputs", "expected_task", "expected_summary"),
    [
        ("false", "true", "<redacted (11 chars)>", "SUMMARY_SECRET"),
        ("true", "false", "TASK_SECRET", "<redacted (14 chars)>"),
    ],
)
def test_subagent_task_and_summary_privacy_are_independent(
    captured_spans,
    monkeypatch,
    log_prompts,
    log_model_outputs,
    expected_task,
    expected_summary,
):
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", log_prompts)
    monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", log_model_outputs)
    common = {"conversation_id": "privacy", "generation_id": "subagent-privacy", "subagent_type": "explore"}
    _dispatch("subagentStart", {**common, "subagent_id": "s", "task": "TASK_SECRET"})
    _dispatch(
        "subagentStop",
        {**common, "task": "TASK_SECRET", "summary": "SUMMARY_SECRET", "status": "completed"},
    )

    attrs = _attrs(_span(captured_spans[0]))
    assert attrs["input.value"] == expected_task
    assert attrs["output.value"] == expected_summary


def test_subagent_stop_reapplies_current_prompt_privacy(captured_spans, monkeypatch):
    common = {"conversation_id": "privacy", "generation_id": "privacy-change", "subagent_type": "explore"}
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    _dispatch("subagentStart", {**common, "subagent_id": "s", "task": "TASK_SECRET"})
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
    _dispatch(
        "subagentStop",
        {**common, "task": "TASK_SECRET", "summary": "safe", "status": "completed"},
    )

    assert _attrs(_span(captured_spans[0]))["input.value"] == "<redacted (11 chars)>"


@pytest.mark.parametrize(
    ("log_prompts", "log_model_outputs", "expected_prompt", "expected_output"),
    [
        ("false", "true", "<redacted (13 chars)>", "MODEL_SECRET"),
        ("true", "false", "PROMPT_SECRET", "<redacted (12 chars)>"),
    ],
)
def test_prompt_and_model_output_privacy_are_independent(
    captured_spans,
    monkeypatch,
    log_prompts,
    log_model_outputs,
    expected_prompt,
    expected_output,
):
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", log_prompts)
    monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", log_model_outputs)
    common = {"conversation_id": "privacy", "generation_id": f"g-{log_prompts}"}

    _dispatch("beforeSubmitPrompt", {**common, "prompt": "PROMPT_SECRET"})
    _dispatch("afterAgentResponse", {**common, "text": "MODEL_SECRET"})

    attrs = _attrs(_span(captured_spans[0]))
    assert attrs["input.value"] == expected_prompt
    assert attrs["output.value"] == expected_output


def test_missing_generation_emits_without_shared_root_state(captured_spans, _state_dir):
    _dispatch(
        "beforeSubmitPrompt",
        {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "conversation-only",
            "prompt": "safe fallback",
        },
    )

    assert len(captured_spans) == 1
    assert not list(_state_dir.glob("root_*"))


def test_all_string_attributes_are_bounded(captured_spans, monkeypatch):
    monkeypatch.setattr(adapter, "MAX_ATTR_CHARS", 8)
    _dispatch(
        "postToolUse",
        {
            "conversation_id": "conversation-too-long",
            "generation_id": "g",
            "tool_name": "CustomLongTool",
            "tool_use_id": "x",
            "tool_input": "0123456789",
            "tool_output": "abcdefghij",
        },
    )
    span = _span(captured_spans[0])
    for attribute in span["attributes"]:
        value = attribute["value"].get("stringValue")
        if value is not None:
            assert len(value) <= 8
