"""Cursor 2.5+ hook contract regressions grounded in official docs and CLI bundle."""

from __future__ import annotations

import json
import threading
import time
from unittest import mock

import pytest

from tracing.cursor.hooks import adapter
from tracing.cursor.hooks.handlers import _dispatch, _subagent_state_key, _tool_state_key


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


def test_json_string_surrogates_are_safe_for_all_correlation_keys(captured_spans):
    surrogate = "identity-\ud800"
    assert len(_tool_state_key(surrogate, surrogate).rsplit("_", 1)[-1]) == 64
    assert _subagent_state_key(surrogate, surrogate, surrogate).startswith("subagent_g_")

    _dispatch(
        "beforeSubmitPrompt",
        {"conversation_id": "c", "generation_id": surrogate, "prompt": "prompt"},
    )
    _dispatch(
        "afterAgentResponse",
        {"conversation_id": "c", "generation_id": surrogate, "text": surrogate},
    )
    assert captured_spans


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


def test_generic_tool_ids_that_sanitize_identically_do_not_cross_pair(captured_spans):
    common = {"conversation_id": "c", "generation_id": "g", "tool_name": "Grep"}
    _dispatch("preToolUse", {**common, "tool_use_id": "call/a", "tool_input": "INPUT_A"})
    _dispatch("preToolUse", {**common, "tool_use_id": "call?a", "tool_input": "INPUT_B"})
    _dispatch("postToolUse", {**common, "tool_use_id": "call/a", "tool_output": "OUTPUT_A"})
    _dispatch("postToolUse", {**common, "tool_use_id": "call?a", "tool_output": "OUTPUT_B"})

    spans = [_span(payload) for payload in captured_spans]
    assert [(_attrs(span)["input.value"], _attrs(span)["output.value"]) for span in spans] == [
        ("INPUT_A", "OUTPUT_A"),
        ("INPUT_B", "OUTPUT_B"),
    ]


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
    # Failures must report OTLP ERROR status so backends count them as errors.
    assert span["status"] == {"code": 2, "message": "exit 1"}
    remaining = list(_state_dir.rglob("tool_*_privacy.stack.json"))
    assert len(remaining) == 1
    assert all("_privacy.stack.json" in path.name for path in remaining)
    tombstone_text = "\n".join(path.read_text() for path in remaining)
    assert '"command"' not in tombstone_text
    assert "exit 1" not in tombstone_text


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
    remaining = list(_state_dir.rglob("subagent_*.stack.json"))
    assert len(remaining) == 2
    assert all("_privacy.stack.json" in path.name for path in remaining)
    tombstone_text = "\n".join(path.read_text() for path in remaining)
    assert "first" not in tombstone_text
    assert "second" not in tombstone_text
    assert '"A"' not in tombstone_text
    assert '"B"' not in tombstone_text


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


def test_sequential_shell_start_cannot_expose_delayed_redacted_duplicate(captured_spans, monkeypatch):
    common = {"conversation_id": "c", "generation_id": "g"}
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
    first = {**common, "command": "A_COMMAND_SECRET", "output": "A_OUTPUT_SECRET"}
    _dispatch("beforeShellExecution", first)
    _dispatch("afterShellExecution", first)

    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    _dispatch("beforeShellExecution", {**common, "command": "B_COMMAND"})
    _dispatch("afterShellExecution", first)

    exported = json.dumps(captured_spans)
    assert "A_COMMAND_SECRET" not in exported
    assert "A_OUTPUT_SECRET" not in exported


def test_sequential_mcp_start_cannot_expose_delayed_redacted_duplicate(captured_spans, monkeypatch):
    common = {"conversation_id": "c", "generation_id": "g"}
    first = {**common, "tool_name": "alpha", "tool_input": "A_INPUT_SECRET"}
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
    _dispatch("beforeMCPExecution", first)
    _dispatch("afterMCPExecution", {**first, "result": "A_OUTPUT_SECRET"})

    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
    _dispatch("beforeMCPExecution", {**common, "tool_name": "beta", "tool_input": "B_INPUT"})
    _dispatch("afterMCPExecution", {**first, "result": "A_OUTPUT_SECRET"})

    exported = json.dumps(captured_spans)
    assert "A_INPUT_SECRET" not in exported
    assert "A_OUTPUT_SECRET" not in exported


@pytest.mark.parametrize("completion_event", ["postToolUse", "postToolUseFailure"])
def test_failed_generic_send_retries_without_losing_private_state(monkeypatch, completion_event):
    common = {
        "conversation_id": "c",
        "generation_id": "g",
        "tool_name": "Custom",
        "tool_use_id": "call",
        "tool_input": "INPUT_SECRET",
    }
    completion = {**common}
    if completion_event == "postToolUse":
        completion["tool_output"] = "OUTPUT_SECRET"
    else:
        completion.update(error_message="OUTPUT_SECRET", failure_type="error")
    attempts = []

    def fail_once(payload):
        attempts.append(payload)
        return len(attempts) > 1

    _dispatch("preToolUse", common)
    with mock.patch("tracing.cursor.hooks.handlers._send_span_to_backend", side_effect=fail_once):
        _dispatch(completion_event, completion)
        _dispatch(completion_event, completion)

    assert len(attempts) == 2
    attrs = _attrs(_span(attempts[1]))
    assert attrs["input.value"] == "INPUT_SECRET"
    assert attrs["output.value"] == "OUTPUT_SECRET"


def test_concurrent_generic_completions_export_once(monkeypatch):
    common = {
        "conversation_id": "c",
        "generation_id": "g",
        "tool_name": "Custom",
        "tool_use_id": "call",
        "tool_input": "input",
        "tool_output": "output",
    }
    attempts = []
    entered = threading.Event()

    def slow_send(payload):
        attempts.append(payload)
        entered.set()
        time.sleep(0.05)
        return True

    _dispatch("preToolUse", common)
    with mock.patch("tracing.cursor.hooks.handlers._send_span_to_backend", side_effect=slow_send):
        threads = [threading.Thread(target=lambda: _dispatch("postToolUse", common)) for _ in range(2)]
        for thread in threads:
            thread.start()
        assert entered.wait(5)
        for thread in threads:
            thread.join(5)

    assert len(attempts) == 1


def test_generic_only_shell_delivery_is_not_discarded(captured_spans):
    common = {
        "conversation_id": "c",
        "generation_id": "g",
        "tool_name": "Shell",
        "tool_use_id": "shell-call",
        "tool_input": {"command": "pwd"},
    }
    _dispatch("preToolUse", common)
    _dispatch("postToolUse", {**common, "tool_output": "/tmp"})
    assert [_span(payload)["name"] for payload in captured_spans] == ["Tool: Shell"]


def test_dedicated_shell_success_suppresses_matching_generic_completion(captured_spans):
    common = {"conversation_id": "c", "generation_id": "g"}
    _dispatch("beforeShellExecution", {**common, "command": "pwd"})
    _dispatch("afterShellExecution", {**common, "command": "pwd", "output": "/tmp"})
    generic = {
        **common,
        "tool_name": "Shell",
        "tool_use_id": "shell-call",
        "tool_input": {"command": "pwd"},
        "tool_output": "/tmp",
    }
    _dispatch("preToolUse", generic)
    _dispatch("postToolUse", generic)
    assert [_span(payload)["name"] for payload in captured_spans] == ["Shell"]


def test_failed_dedicated_shell_send_preserves_generic_fallback(monkeypatch):
    common = {"conversation_id": "c", "generation_id": "g"}
    attempts = []

    def reject_rich(payload):
        attempts.append(payload)
        return _span(payload)["name"] != "Shell"

    with mock.patch("tracing.cursor.hooks.handlers._send_span_to_backend", side_effect=reject_rich):
        _dispatch("beforeShellExecution", {**common, "command": "pwd"})
        _dispatch("afterShellExecution", {**common, "command": "pwd", "output": "/tmp"})
        generic = {
            **common,
            "tool_name": "Shell",
            "tool_use_id": "shell-call",
            "tool_input": {"command": "pwd"},
            "tool_output": "/tmp",
        }
        _dispatch("preToolUse", generic)
        _dispatch("postToolUse", generic)

    assert [_span(payload)["name"] for payload in attempts] == ["Shell", "Tool: Shell"]


@pytest.mark.parametrize("event,terminal_name", [("stop", "Agent Stop"), ("sessionEnd", "Session End")])
def test_failed_terminal_send_retries_only_unconfirmed_spans(monkeypatch, event, terminal_name):
    common = {"conversation_id": "c", "generation_id": "g"}
    _dispatch("beforeSubmitPrompt", {**common, "prompt": "PRIVATE_PROMPT"})
    _dispatch("afterAgentResponse", {**common, "text": "PRIVATE_RESPONSE"})
    attempts = []
    terminal_attempts = 0

    def fail_terminal_once(payload):
        nonlocal terminal_attempts
        name = _span(payload)["name"]
        attempts.append(payload)
        if name == terminal_name:
            terminal_attempts += 1
            return terminal_attempts > 1
        return True

    with mock.patch("tracing.cursor.hooks.handlers._send_span_to_backend", side_effect=fail_terminal_once):
        _dispatch(event, {**common, "input_tokens": 7, "output_tokens": 3})
        _dispatch(event, {**common, "input_tokens": 7, "output_tokens": 3})

    names = [_span(payload)["name"] for payload in attempts]
    assert names == ["User Prompt", "Agent Response", terminal_name, terminal_name]
    llm_attrs = _attrs(_span(attempts[1]))
    assert llm_attrs["input.value"] == "PRIVATE_PROMPT"
    assert llm_attrs["output.value"] == "PRIVATE_RESPONSE"
    assert llm_attrs["llm.token_count.total"] == 10
    assert adapter.generation_is_completed("g") is True


@pytest.mark.parametrize("event", ["stop", "sessionEnd"])
def test_token_counters_reject_bool_negative_and_fraction_independently(captured_spans, event):
    _dispatch(
        event,
        {
            "conversation_id": "c",
            "input_tokens": True,
            "output_tokens": -5,
            "cache_read_tokens": 1.9,
            "cache_write_tokens": "4",
            "duration_ms": 1.9,
        },
    )
    attrs = _attrs(_span(captured_spans[0]))
    assert "llm.token_count.prompt" not in attrs
    assert "llm.token_count.completion" not in attrs
    assert "llm.token_count.prompt_details.cache_read" not in attrs
    assert attrs["llm.token_count.prompt_details.cache_write"] == 4
    duration_key = "cursor.stop.duration_ms" if event == "stop" else "cursor.session.duration_ms"
    assert attrs[duration_key] == 1


@pytest.mark.parametrize("event", ["stop", "sessionEnd"])
def test_generationless_terminal_transport_failure_retries(monkeypatch, event):
    payload = {"conversation_id": "legacy-conversation"}
    attempts = []

    def fail_once(span):
        attempts.append(span)
        return len(attempts) > 1

    with mock.patch("tracing.cursor.hooks.handlers._send_span_to_backend", side_effect=fail_once):
        _dispatch(event, payload)
        _dispatch(event, payload)

    assert len(attempts) == 2
    digest = adapter.stable_digest(f"cursor-terminal-fallback\0{event}\0legacy-conversation")
    assert adapter.generation_digest_completion_status(digest) == "completed"


@pytest.mark.parametrize("event", ["stop", "sessionEnd"])
def test_generationless_terminal_ledger_failure_does_not_resend(monkeypatch, event):
    payload = {"conversation_id": "legacy-conversation"}
    attempts = []

    with (
        mock.patch(
            "tracing.cursor.hooks.handlers._send_span_to_backend",
            side_effect=lambda span: attempts.append(span) or True,
        ),
        mock.patch(
            "tracing.cursor.hooks.handlers.generation_mark_digest_completed",
            side_effect=[RuntimeError("ledger unavailable"), None],
        ) as mark,
    ):
        with pytest.raises(RuntimeError, match="ledger unavailable"):
            _dispatch(event, payload)
        _dispatch(event, payload)

    assert len(attempts) == 1
    assert mark.call_count == 2


def test_shell_without_command_keeps_redaction_for_delayed_duplicate(monkeypatch):
    payload = {"conversation_id": "c", "generation_id": "g"}
    sent = []
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
    with mock.patch(
        "tracing.cursor.hooks.handlers._send_span_to_backend",
        side_effect=lambda span: sent.append(span) or True,
    ):
        _dispatch("beforeShellExecution", {**payload, "command": "SECRET_COMMAND"})
        after = {**payload, "output": "SECRET_OUTPUT", "exit_code": 0}
        _dispatch("afterShellExecution", after)
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        _dispatch("afterShellExecution", after)

    exported = json.dumps(sent)
    assert "SECRET_COMMAND" not in exported
    assert "SECRET_OUTPUT" not in exported


def test_llm_usage_ledger_failure_retries_without_duplicate_tokens(monkeypatch):
    payload = {"conversation_id": "c", "generation_id": "g"}
    sent = []
    with (
        mock.patch(
            "tracing.cursor.hooks.handlers._send_span_to_backend",
            side_effect=lambda span: sent.append(span) or True,
        ),
        mock.patch(
            "tracing.cursor.hooks.handlers.generation_terminal_attribution_note_usage",
            side_effect=[RuntimeError("usage ledger unavailable"), None],
        ),
    ):
        _dispatch("beforeSubmitPrompt", {**payload, "prompt": "p"})
        _dispatch("afterAgentResponse", {**payload, "response": "r"})
        terminal = {**payload, "input_tokens": 7, "output_tokens": 3}
        with pytest.raises(RuntimeError, match="usage ledger unavailable"):
            _dispatch("stop", terminal)
        _dispatch("stop", terminal)

    spans = [_span(item) for item in sent]
    token_bearing = [span for span in spans if any(key.startswith("llm.token_count.") for key in _attrs(span))]
    assert [span["name"] for span in token_bearing] == ["Agent Response"]
    assert [span["name"] for span in spans].count("Agent Response") == 1


@pytest.mark.parametrize(
    ("dedicated_event", "tool_name", "tool_input", "rich_payload", "rich_span_name"),
    [
        ("beforeReadFile", "read_file", {"path": "/tmp/a"}, {"file_path": "/tmp/a"}, "Read File"),
        (
            "afterFileEdit",
            "edit_file",
            {"path": "/tmp/a", "edits": "change"},
            {"file_path": "/tmp/a", "edits": "change"},
            "File Edit",
        ),
        ("beforeTabFileRead", "tab_file_read", {"path": "/tmp/a"}, {"file_path": "/tmp/a"}, "Tab Read File"),
        (
            "afterTabFileEdit",
            "tab_file_edit",
            {"path": "/tmp/a", "edits": "change"},
            {"file_path": "/tmp/a", "edits": "change"},
            "Tab File Edit",
        ),
    ],
)
def test_all_dedicated_file_families_suppress_matching_generic(
    captured_spans, dedicated_event, tool_name, tool_input, rich_payload, rich_span_name
):
    payload = {"conversation_id": "c", "generation_id": "g", "tool_use_id": "id", "tool_name": tool_name}
    _dispatch("preToolUse", {**payload, "tool_input": tool_input})
    _dispatch(dedicated_event, {"conversation_id": "c", "generation_id": "g", "tool_use_id": "id", **rich_payload})
    _dispatch("postToolUse", {**payload, "tool_output": "ok"})
    assert [_span(item)["name"] for item in captured_spans] == [rich_span_name]


def test_terminal_preflights_exact_ledger_capacity(monkeypatch, captured_spans):
    monkeypatch.setattr(adapter, "COMPLETION_LEDGER_MAX_ROWS", 2)
    adapter.generation_mark_completed("already-complete")
    payload = {"conversation_id": "c", "generation_id": "g"}
    _dispatch("beforeSubmitPrompt", {**payload, "prompt": "p"})
    _dispatch("afterAgentResponse", {**payload, "response": "r"})
    before = len(captured_spans)
    _dispatch("stop", {**payload, "input_tokens": 1, "output_tokens": 1})
    assert len(captured_spans) == before
    assert adapter.generation_completion_status("g") == "active"


def test_failed_deferred_root_is_retried_before_terminal_completion(monkeypatch):
    payload = {"conversation_id": "c", "generation_id": "g"}
    attempts = []
    root_attempts = 0

    def fail_first_root(span):
        nonlocal root_attempts
        name = _span(span)["name"]
        attempts.append(name)
        if name == "User Prompt":
            root_attempts += 1
            return root_attempts > 1
        return True

    with mock.patch("tracing.cursor.hooks.handlers._send_span_to_backend", side_effect=fail_first_root):
        _dispatch("beforeSubmitPrompt", {**payload, "prompt": "p"})
        _dispatch("afterAgentResponse", {**payload, "response": "r"})
        _dispatch("stop", payload)

    assert attempts == ["User Prompt", "User Prompt", "Agent Response", "Agent Stop"]
    assert adapter.generation_is_completed("g") is True


def test_root_retry_reapplies_current_privacy_policy(monkeypatch):
    payload = {"conversation_id": "c", "generation_id": "g"}
    roots = []
    monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
    monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "true")

    def fail_first_root(span):
        if _span(span)["name"] == "User Prompt":
            roots.append(span)
            return len(roots) > 1
        return True

    with mock.patch("tracing.cursor.hooks.handlers._send_span_to_backend", side_effect=fail_first_root):
        _dispatch("beforeSubmitPrompt", {**payload, "prompt": "PROMPT_SECRET"})
        _dispatch("afterAgentResponse", {**payload, "response": "RESPONSE_SECRET"})
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "false")
        _dispatch("stop", payload)

    retried = json.dumps(roots[-1])
    assert "PROMPT_SECRET" not in retried
    assert "RESPONSE_SECRET" not in retried


def test_pending_generation_state_does_not_persist_raw_identities(monkeypatch, _state_dir):
    conversation = "CONVERSATION_SENTINEL"
    user = "USER_SENTINEL@example.com"
    payload = {"conversation_id": conversation, "generation_id": "g", "user_email": user}
    with mock.patch("tracing.cursor.hooks.handlers._send_span_to_backend", return_value=False):
        _dispatch("beforeSubmitPrompt", {**payload, "prompt": "p"})
        _dispatch("afterAgentResponse", {**payload, "response": "r"})

    persisted = b"".join(path.read_bytes() for path in _state_dir.rglob("*") if path.is_file())
    assert conversation.encode() not in persisted
    assert user.encode() not in persisted


@pytest.mark.parametrize(
    ("dedicated_event", "tool_name", "tool_input", "rich_payload"),
    [
        ("beforeReadFile", "read_file", {"path": "/tmp/a"}, {"file_path": "/tmp/a"}),
        ("afterFileEdit", "edit_file", {"path": "/tmp/a", "edits": "x"}, {"file_path": "/tmp/a", "edits": "x"}),
        ("beforeTabFileRead", "tab_file_read", {"path": "/tmp/a"}, {"file_path": "/tmp/a"}),
        ("afterTabFileEdit", "tab_file_edit", {"path": "/tmp/a", "edits": "x"}, {"file_path": "/tmp/a", "edits": "x"}),
    ],
)
def test_uncorrelatable_dedicated_file_event_does_not_suppress_distinct_generic(
    captured_spans, dedicated_event, tool_name, tool_input, rich_payload
):
    generic = {"conversation_id": "c", "generation_id": "g", "tool_use_id": "generic-a", "tool_name": tool_name}
    _dispatch("preToolUse", {**generic, "tool_input": tool_input})
    _dispatch(dedicated_event, {"conversation_id": "c", "generation_id": "g", **rich_payload})
    _dispatch("postToolUse", {**generic, "tool_output": "ok"})
    assert len(captured_spans) == 2


@pytest.mark.parametrize("event,span_name", [("stop", "Agent Stop"), ("sessionEnd", "Session End")])
def test_terminal_only_usage_ledger_failure_does_not_resend(monkeypatch, event, span_name):
    payload = {"conversation_id": "c", "generation_id": "g", "input_tokens": 7, "output_tokens": 3}
    sent = []
    with (
        mock.patch(
            "tracing.cursor.hooks.handlers._send_span_to_backend",
            side_effect=lambda span: sent.append(span) or True,
        ),
        mock.patch(
            "tracing.cursor.hooks.handlers.generation_terminal_attribution_note_usage",
            side_effect=[RuntimeError("usage ledger unavailable"), None],
        ),
    ):
        with pytest.raises(RuntimeError, match="usage ledger unavailable"):
            _dispatch(event, payload)
        _dispatch(event, payload)

    spans = [_span(item) for item in sent]
    assert [span["name"] for span in spans] == [span_name]
    assert _attrs(spans[0])["llm.token_count.total"] == 10


def test_failed_dedicated_shell_fallback_keeps_command_details_redacted(monkeypatch):
    secret = "COMMAND_DETAILS_SECRET"
    payload = {"conversation_id": "c", "generation_id": "g"}
    exported = []
    monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
    monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")

    def fail_dedicated(span):
        name = _span(span)["name"]
        if name == "Shell":
            return False
        exported.append(span)
        return True

    with mock.patch("tracing.cursor.hooks.handlers._send_span_to_backend", side_effect=fail_dedicated):
        _dispatch("beforeShellExecution", {**payload, "command": secret})
        _dispatch("afterShellExecution", {**payload, "command": secret, "output": "ok"})
        generic = {**payload, "tool_use_id": "shell-1", "tool_name": "Shell"}
        _dispatch("preToolUse", {**generic, "tool_input": {"command": secret}})
        _dispatch("postToolUse", {**generic, "tool_input": {"command": secret}, "tool_output": "ok"})

    fallback = next(_span(item) for item in exported if _span(item)["name"] == "Tool: Shell")
    assert secret not in json.dumps(fallback)
    assert _attrs(fallback)["input.value"].startswith("<redacted (")
