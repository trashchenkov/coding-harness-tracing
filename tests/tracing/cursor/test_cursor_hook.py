#!/usr/bin/env python3
"""Tests for tracing.cursor.hooks.handlers and the current Cursor hook inventory."""

import io
import itertools
import json
import os
import sqlite3
import sys
import threading
from unittest import mock

import pytest

from tracing.cursor.hooks import adapter
from tracing.cursor.hooks.handlers import (
    _dispatch,
    _event_name,
    _jq_str,
    _print_permissive,
    _sweep_pending_generation_cleanups,
    _trace_id_from_event,
    main,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    """Mock time.sleep to prevent real delays while tracking calls."""
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


@pytest.fixture(autouse=True)
def _patch_cursor_state(tmp_path, monkeypatch):
    """Redirect cursor adapter STATE_DIR to temp."""
    state_dir = tmp_path / "state" / "cursor"
    state_dir.mkdir(parents=True)
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    return state_dir


@pytest.fixture
def captured_spans():
    """Mock send_span and collect all payloads sent."""
    sent = []

    def record(payload):
        sent.append(payload)
        # send_span reports delivery; returning True keeps the double captured
        # here faithful to the real contract handlers branch on.
        return True

    with mock.patch("tracing.cursor.hooks.handlers.send_span", side_effect=record):
        yield sent


# ---------------------------------------------------------------------------
# _print_permissive tests
# ---------------------------------------------------------------------------


class TestPrintPermissive:

    def test_before_submit_prompt_returns_continue_true(self):
        """beforeSubmitPrompt uses its documented continuation response."""
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("beforeSubmitPrompt")
        assert json.loads(buf.getvalue()) == {"continue": True}

    def test_before_shell_event(self):
        """beforeShellExecution also returns permission allow."""
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("beforeShellExecution")
        assert json.loads(buf.getvalue()) == {"permission": "allow"}

    def test_observational_event_returns_empty_object(self):
        """Hooks with no control fields return valid empty JSON."""
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("afterAgentResponse")
        assert json.loads(buf.getvalue()) == {}

    def test_pre_tool_use_returns_permission_allow(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("preToolUse")
        assert json.loads(buf.getvalue()) == {"permission": "allow"}

    def test_subagent_start_returns_permission_allow(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("subagentStart")
        assert json.loads(buf.getvalue()) == {"permission": "allow"}

    def test_workspace_open_returns_empty_plugin_paths(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("workspaceOpen")
        assert json.loads(buf.getvalue()) == {"pluginPaths": []}

    def test_empty_event_returns_empty_object(self):
        buf = io.StringIO()
        with mock.patch.object(sys, "__stdout__", buf):
            _print_permissive("")
        assert json.loads(buf.getvalue()) == {}


# ---------------------------------------------------------------------------
# _jq_str tests
# ---------------------------------------------------------------------------


class TestJqStr:

    def test_returns_first_matching_key(self):
        d = {"prompt": "hello", "input": "world"}
        assert _jq_str(d, "prompt", "input") == "hello"

    def test_skips_to_second_key(self):
        d = {"input": "world"}
        assert _jq_str(d, "prompt", "input") == "world"

    def test_returns_default_when_no_match(self):
        assert _jq_str({}, "a", "b", default="fallback") == "fallback"

    def test_returns_empty_default(self):
        assert _jq_str({}, "a") == ""

    def test_skips_none_value(self):
        d = {"a": None, "b": "found"}
        assert _jq_str(d, "a", "b") == "found"

    def test_skips_empty_string_value(self):
        d = {"a": "", "b": "found"}
        assert _jq_str(d, "a", "b") == "found"

    def test_converts_non_string_to_str(self):
        d = {"count": 42}
        assert _jq_str(d, "count") == "42"

    def test_all_none_returns_default(self):
        d = {"a": None, "b": None}
        assert _jq_str(d, "a", "b", default="x") == "x"


# ---------------------------------------------------------------------------
# _dispatch tests
# ---------------------------------------------------------------------------


class TestDispatch:

    def test_routes_to_correct_handler(self, monkeypatch):
        """Known event routes to correct handler function."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_before_submit_prompt") as h,
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                },
            )
            h.assert_called_once()

    def test_routes_after_agent_response(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_after_agent_response") as h,
        ):
            _dispatch("afterAgentResponse", {"conversation_id": "c1", "generation_id": "g1"})
            h.assert_called_once()

    @pytest.mark.parametrize("ledger_status", ["ledger-saturated", "ledger-unavailable"])
    def test_missing_generation_drops_when_ledger_is_globally_fail_closed(self, monkeypatch, ledger_status):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch(
                "tracing.cursor.hooks.handlers.generation_completion_status", return_value=ledger_status
            ) as completion_status,
            mock.patch("tracing.cursor.hooks.handlers._handle_after_agent_thought") as handler,
            mock.patch("tracing.cursor.hooks.handlers.log") as log_mock,
        ):
            _dispatch("afterAgentThought", {"conversation_id": "c1", "text": "private"})

        completion_status.assert_called_once_with("")
        handler.assert_not_called()
        assert ledger_status in log_mock.call_args[0][0]

    def test_missing_generation_routes_when_ledger_is_active(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch(
                "tracing.cursor.hooks.handlers.generation_completion_status", return_value="active"
            ) as completion_status,
            mock.patch("tracing.cursor.hooks.handlers._handle_after_agent_thought") as handler,
        ):
            _dispatch("afterAgentThought", {"conversation_id": "c1", "text": "allowed"})

        completion_status.assert_called_once_with("")
        handler.assert_called_once()

    def test_actual_saturated_ledger_drops_missing_generation(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setattr(adapter, "COMPLETION_LEDGER_MAX_ROWS", 1)
        adapter.generation_mark_completed("terminal")

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_after_agent_thought") as handler,
        ):
            _dispatch("afterAgentThought", {"conversation_id": "c1", "text": "private"})

        handler.assert_not_called()

    @pytest.mark.parametrize("generation_id", [None, "g-active"])
    def test_status_to_handler_is_atomic_with_global_saturation(self, monkeypatch, generation_id):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setattr(adapter, "COMPLETION_LEDGER_MAX_ROWS", 1)
        checked_active = threading.Event()
        resume_dispatch = threading.Event()
        mark_done = threading.Event()
        handler_called = threading.Event()
        original_status = getattr(adapter, "generation_completion_status")
        expected_status_id = generation_id or ""

        def pause_after_active(gen_id):
            status = original_status(gen_id)
            if gen_id == expected_status_id and status == "active":
                checked_active.set()
                assert resume_dispatch.wait(timeout=5)
            return status

        def mark_terminal():
            adapter.generation_mark_completed("terminal")
            mark_done.set()

        event = {"conversation_id": "c1", "text": "private"}
        if generation_id is not None:
            event["generation_id"] = generation_id
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.generation_completion_status", side_effect=pause_after_active),
            mock.patch(
                "tracing.cursor.hooks.handlers._handle_after_agent_thought",
                side_effect=lambda *_args: handler_called.set(),
            ),
        ):
            dispatch_thread = threading.Thread(target=lambda: _dispatch("afterAgentThought", event))
            mark_thread = threading.Thread(target=mark_terminal)
            dispatch_thread.start()
            assert checked_active.wait(timeout=5)
            mark_thread.start()
            mark_was_blocked = not mark_done.wait(timeout=0.1)
            resume_dispatch.set()
            dispatch_thread.join(timeout=5)
            mark_thread.join(timeout=5)

        assert mark_was_blocked
        assert handler_called.is_set()
        assert mark_done.is_set()
        assert not dispatch_thread.is_alive()
        assert not mark_thread.is_alive()

    def test_routes_stop(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_stop") as h,
        ):
            _dispatch("stop", {"conversation_id": "c1", "generation_id": "g1"})
            h.assert_called_once()

    def test_unknown_event_logs_warning(self, monkeypatch):
        """Unknown event logs warning, no crash."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.log") as log_mock,
        ):
            _dispatch("unknownEvent", {"conversation_id": "c1", "generation_id": "g1"})
            log_mock.assert_called_once()
            assert "Unknown" in log_mock.call_args[0][0]

    def test_tracing_disabled_returns_early(self, monkeypatch):
        """Tracing disabled -> returns without dispatching."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        with mock.patch("tracing.cursor.hooks.handlers._handle_before_submit_prompt") as h:
            _dispatch("beforeSubmitPrompt", {"conversation_id": "c1", "generation_id": "g1"})
            h.assert_not_called()

    def test_no_backend_send_fails_gracefully(self, monkeypatch):
        """send_span failure doesn't crash — IDE defers root to afterAgentResponse and LLM to stop."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.send_span", return_value=False) as send_mock,
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "c1",
                    "generation_id": "g1",
                },
            )
            assert send_mock.call_count == 0
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "response": "done",
                },
            )
            # afterAgentResponse sends the deferred root User Prompt only; LLM is deferred to stop.
            assert send_mock.call_count == 1
            _dispatch(
                "stop",
                {
                    "hook_event_name": "stop",
                    "conversation_id": "c1",
                    "generation_id": "g1",
                },
            )
            # The deferred LLM transport failed, so terminal delivery is not
            # attempted and retryable generation state is retained.
            assert send_mock.call_count == 2
            assert adapter.generation_is_completed("g1") is False


# ---------------------------------------------------------------------------
# _handle_before_submit_prompt tests
# ---------------------------------------------------------------------------


class TestHandleBeforeSubmitPrompt:

    def test_legacy_camel_case_payload_uses_same_deferred_topology(self, captured_spans, monkeypatch):
        """Compatibility aliases do not select a different host topology."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="aabb" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_save") as save_mock,
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hookEventName": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "fix the bug",
                },
            )

        save_mock.assert_called_once_with("gen-1", "aabb" * 4)
        assert captured_spans == []

    def test_documented_payload_defers_root_chain_to_after_response(self, captured_spans, monkeypatch):
        """Documented payload defers root CHAIN; LLM completion is deferred to stop."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="aabb" * 4),
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "fix the bug",
                    "model_name": "claude-4",
                },
            )

        assert len(captured_spans) == 0

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000):
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "I fixed the bug",
                    "model_name": "claude-4",
                },
            )

        # Only the deferred root User Prompt CHAIN is sent at afterAgentResponse.
        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["User Prompt"]
        root = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        root_attrs = {a["key"]: a["value"] for a in root["attributes"]}
        assert root_attrs["input.value"]["stringValue"] == "fix the bug"
        assert root_attrs["output.value"]["stringValue"] == "I fixed the bug"
        assert root["startTimeUnixNano"].startswith("5000")
        assert root["endTimeUnixNano"].startswith("9000")

        # Agent Response LLM span appears at stop, carrying the same input/output.
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=10000):
            _dispatch(
                "stop",
                {
                    "hook_event_name": "stop",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                },
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["User Prompt", "Agent Response", "Agent Stop"]
        llm = captured_spans[1]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        llm_attrs = {a["key"]: a["value"] for a in llm["attributes"]}
        assert llm_attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert llm_attrs["input.value"]["stringValue"] == "fix the bug"
        assert llm_attrs["output.value"]["stringValue"] == "I fixed the bug"


# ---------------------------------------------------------------------------
# _handle_after_agent_response tests
# ---------------------------------------------------------------------------


class TestHandleAfterAgentResponse:

    def test_defers_llm_span_until_stop(self, captured_spans, monkeypatch):
        """afterAgentResponse defers the LLM span; it is emitted only at stop."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="ccdd" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent123"),
        ):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "I found the issue",
                    "model_name": "claude-4",
                },
            )

        # afterAgentResponse no longer sends a span immediately
        assert len(captured_spans) == 0

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="eeff" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent123"),
        ):
            _dispatch(
                "stop",
                {"hook_event_name": "stop", "conversation_id": "conv-1", "generation_id": "gen-1"},
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["Agent Response", "Agent Stop"]

        llm_span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in llm_span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert attrs["output.value"]["stringValue"] == "I found the issue"
        assert llm_span["parentSpanId"] == "parent123"

    def test_defers_llm_span_with_full_attributes(self, captured_spans, monkeypatch):
        """Deferred LLM span carries input, output, session.id, model_name when flushed at stop."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=4000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parentX"),
        ):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-9",
                    "generation_id": "gen-9",
                    "prompt": "do the thing",
                    "model_name": "claude-4",
                },
            )
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-9",
                    "generation_id": "gen-9",
                    "response": "did the thing",
                    "model_name": "claude-4",
                },
            )

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=8000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parentX"),
        ):
            _dispatch("stop", {"conversation_id": "conv-9", "generation_id": "gen-9"})

        llm_span = next(
            s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            for s in captured_spans
            if s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Agent Response"
        )
        attrs = {a["key"]: a["value"] for a in llm_span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert attrs["input.value"]["stringValue"] == "do the thing"
        assert attrs["output.value"]["stringValue"] == "did the thing"
        assert attrs["session.id"]["stringValue"] == "conv-9"
        assert attrs["cursor.conversation.id"]["stringValue"] == "conv-9"
        assert attrs["llm.model_name"]["stringValue"] == "claude-4"

    def test_defers_llm_span_preserves_after_agent_response_timing(self, captured_spans, monkeypatch):
        """Deferred LLM span uses the start_ms recorded at afterAgentResponse, not stop's now_ms."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2500),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "yo",
                },
            )

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9999),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch("stop", {"conversation_id": "conv-1", "generation_id": "gen-1"})

        llm_span = next(
            s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            for s in captured_spans
            if s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Agent Response"
        )
        # start_ms recorded at afterAgentResponse (2500), not at stop (9999).
        assert llm_span["startTimeUnixNano"] == "2500000000"
        assert llm_span["endTimeUnixNano"] == "2500000000"

    def test_no_gen_id_sends_llm_span_immediately(self, captured_spans, monkeypatch):
        """Without gen_id state can't be keyed, so the LLM span is sent immediately (fallback)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="ccdd" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "response": "I found the issue",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == "Agent Response"
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "LLM"
        assert attrs["output.value"]["stringValue"] == "I found the issue"


# ---------------------------------------------------------------------------
# _handle_after_shell_execution tests
# ---------------------------------------------------------------------------


class TestHandleAfterShellExecution:

    def test_creates_tool_span_with_popped_state(self, captured_spans, monkeypatch):
        """Creates TOOL span, merges with before state from state_pop."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        popped = {"command": "ls -la", "cwd": "/tmp", "start_ms": "1000", "trace_id": "t1", "conversation_id": "c1"}
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="eeff" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent1"),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=popped),
        ):
            _dispatch(
                "afterShellExecution",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "output": "total 0",
                    "exit_code": "0",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "shell"
        assert attrs["output.value"]["stringValue"] == "total 0"
        assert attrs["shell.exit_code"]["stringValue"] == "0"
        assert span["name"] == "Shell"

    def test_uses_after_command_when_present(self, captured_spans, monkeypatch):
        """After-event command overrides before-event command."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        popped = {"command": "old_cmd", "start_ms": "1000"}
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=popped),
        ):
            _dispatch(
                "afterShellExecution",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "command": "new_cmd",
                    "output": "ok",
                },
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["input.value"]["stringValue"] == "new_cmd"

    def test_no_popped_state_uses_now(self, captured_spans, monkeypatch):
        """Without popped state, start_ms defaults to now_ms."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
        ):
            _dispatch(
                "afterShellExecution",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "output": "ok",
                },
            )

        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        # start_ms = "3000" -> ns = "3000000000"
        assert span["startTimeUnixNano"] == "3000000000"

    def test_uses_fixture(self, captured_spans, monkeypatch, cursor_after_shell_input):
        """Works with cursor_after_shell fixture."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        fixture = cursor_after_shell_input
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
        ):
            _dispatch(fixture["hook_event_name"], fixture)

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["input.value"]["stringValue"] == "ls -la"
        assert attrs["output.value"]["stringValue"] == "total 0"
        assert attrs["shell.exit_code"]["stringValue"] == "0"


# ---------------------------------------------------------------------------
# _handle_stop tests
# ---------------------------------------------------------------------------


class TestHandleStop:

    def test_creates_chain_span_and_cleans_up(self, captured_spans, monkeypatch):
        """Creates CHAIN span and calls state_cleanup_generation."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="root1"),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation") as cleanup,
        ):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "status": "completed",
                    "loop_count": "3",
                },
            )

        cleanup.assert_called_once_with("gen-1")
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["cursor.stop.status"]["stringValue"] == "completed"
        assert attrs["cursor.stop.loop_count"]["stringValue"] == "3"
        assert span["name"] == "Agent Stop"

    @pytest.mark.parametrize("event", ["stop", "sessionEnd"])
    def test_generationless_terminal_delivery_is_claimed_once(self, captured_spans, monkeypatch, event):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"conversation_id": "c1"}
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(event, payload)
            _dispatch(event, payload)

        assert len(captured_spans) == 1

    @pytest.mark.parametrize("generation_id", ["g1", ""])
    def test_stop_and_session_end_are_distinct_once_only_events(self, captured_spans, monkeypatch, generation_id):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"conversation_id": "c1"}
        if generation_id:
            payload["generation_id"] = generation_id
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch("stop", payload)
            _dispatch("stop", payload)
            _dispatch("sessionEnd", payload)
            _dispatch("sessionEnd", payload)

        names = [span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for span in captured_spans]
        assert names == ["Agent Stop", "Session End"]

    def test_session_end_before_stop_still_emits_deferred_agent_response(self, captured_spans, monkeypatch):
        """sessionEnd-first must flush the deferred LLM entry that cleanup would delete."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"conversation_id": "conv-1", "generation_id": "gen-1"}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch("beforeSubmitPrompt", {**payload, "hook_event_name": "beforeSubmitPrompt", "prompt": "p"})
            _dispatch("afterAgentResponse", {**payload, "hook_event_name": "afterAgentResponse", "response": "r"})
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000):
            _dispatch("sessionEnd", payload)
            _dispatch("sessionEnd", payload)
            _dispatch("stop", payload)
            _dispatch("stop", payload)

        names = [span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for span in captured_spans]
        assert names == ["User Prompt", "Agent Response", "Session End", "Agent Stop"]

    @pytest.mark.parametrize("order", [("stop", "sessionEnd"), ("sessionEnd", "stop")])
    def test_terminal_matrix_parenting_tokens_and_model_once(self, captured_spans, monkeypatch, order):
        """Both terminal orders: correct parents, tokens and model attributed exactly once."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"conversation_id": "conv-1", "generation_id": "gen-1"}
        terminal_extra = {
            "input_tokens": 100,
            "output_tokens": 40,
            "cache_read_tokens": 10,
            "model": "cursor-model-x",
        }
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch("beforeSubmitPrompt", {**payload, "hook_event_name": "beforeSubmitPrompt", "prompt": "p"})
            _dispatch("afterAgentResponse", {**payload, "hook_event_name": "afterAgentResponse", "response": "r"})
        first, second = order
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000):
            _dispatch(first, {**payload, **terminal_extra})
            _dispatch(first, {**payload, **terminal_extra})  # duplicate
            _dispatch(second, {**payload, **terminal_extra})
            _dispatch(second, {**payload, **terminal_extra})  # duplicate

        all_spans = [span["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for span in captured_spans]
        terminal_name = {"stop": "Agent Stop", "sessionEnd": "Session End"}
        assert [span["name"] for span in all_spans] == [
            "User Prompt",
            "Agent Response",
            terminal_name[first],
            terminal_name[second],
        ]
        spans = {span["name"]: span for span in all_spans}

        # One trace; every span parented under the User Prompt root — including
        # the second terminal event, whose root state was already cleaned up.
        root = spans["User Prompt"]
        assert len({span["traceId"] for span in all_spans}) == 1
        assert root.get("parentSpanId", "") == ""
        for name in ("Agent Response", "Agent Stop", "Session End"):
            assert spans[name]["parentSpanId"] == root["spanId"], f"{name} lost its parent"

        # Cumulative usage and model attach exactly once: on the deferred LLM
        # span flushed by the first terminal event, and nowhere else.
        llm_attrs = _attrs(spans["Agent Response"])
        assert llm_attrs["llm.token_count.prompt"]["intValue"] == 110
        assert llm_attrs["llm.token_count.completion"]["intValue"] == 40
        assert llm_attrs["llm.token_count.total"]["intValue"] == 150
        assert llm_attrs["llm.model_name"]["stringValue"] == "cursor-model-x"
        for name in ("Agent Stop", "Session End"):
            chain_attrs = _attrs(spans[name])
            token_keys = [key for key in chain_attrs if key.startswith("llm.token_count.")]
            assert token_keys == [], f"{name} re-attributed usage: {token_keys}"

        # Generation state is fully cleaned up after the terminal family.
        shard = adapter.STATE_DIR / adapter.generation_state_key("gen-1")
        assert not shard.exists()

    @pytest.mark.parametrize("order", [("stop", "sessionEnd"), ("sessionEnd", "stop")])
    def test_second_terminal_attributes_usage_when_first_had_none(self, captured_spans, monkeypatch, order):
        """A tokenless first terminal must not burn the attribution: the later
        terminal event still carries its counts (on its own CHAIN span)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"conversation_id": "conv-1", "generation_id": "gen-1"}
        first, second = order
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000):
            _dispatch(first, payload)
            _dispatch(second, {**payload, "input_tokens": 7, "output_tokens": 3})

        terminal_name = {"stop": "Agent Stop", "sessionEnd": "Session End"}
        spans = {
            span["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"]: span["resourceSpans"][0]["scopeSpans"][0][
                "spans"
            ][0]
            for span in captured_spans
        }
        first_attrs = _attrs(spans[terminal_name[first]])
        assert not any(key.startswith("llm.token_count.") for key in first_attrs)
        second_attrs = _attrs(spans[terminal_name[second]])
        assert second_attrs["llm.token_count.prompt"]["intValue"] == 7
        assert second_attrs["llm.token_count.completion"]["intValue"] == 3

    @pytest.mark.parametrize("generation_id", ["legacy-g1", ""])
    def test_legacy_terminal_tombstone_fails_closed_after_domain_separation(
        self, captured_spans, monkeypatch, generation_id
    ):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"conversation_id": "legacy-c1"}
        if generation_id:
            payload["generation_id"] = generation_id
            adapter.generation_mark_completed(generation_id)
        else:
            legacy_digest = adapter.stable_digest("cursor-terminal-fallback\0legacy-c1")
            adapter.generation_mark_digest_completed(legacy_digest)

        _dispatch("stop", payload)
        _dispatch("sessionEnd", payload)

        assert captured_spans == []

    def test_generationless_tool_and_subagent_emit_inline_without_shared_state(self, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "false")

        tool_common = {
            "conversation_id": "c-no-gen",
            "tool_use_id": "tool-no-gen",
            "tool_name": "browser",
            "tool_input": {"secret": "TOOL_NO_GEN_SECRET"},
        }
        _dispatch("preToolUse", tool_common)
        _dispatch("postToolUse", {**tool_common, "tool_output": "TOOL_NO_GEN_OUTPUT"})
        failed_tool = {
            **tool_common,
            "tool_use_id": "failed-tool-no-gen",
            "error_message": "TOOL_NO_GEN_FAILURE",
        }
        _dispatch("preToolUse", failed_tool)
        _dispatch("postToolUseFailure", failed_tool)

        subagent_common = {
            "conversation_id": "c-no-gen",
            "subagent_type": "explore",
            "task": "SUBAGENT_NO_GEN_SECRET",
        }
        _dispatch("subagentStart", subagent_common)
        _dispatch("subagentStop", {**subagent_common, "summary": "SUBAGENT_NO_GEN_OUTPUT"})

        assert len(captured_spans) == 3
        empty_generation_shard = adapter.STATE_DIR / adapter.generation_state_key("")
        assert not empty_generation_shard.exists()
        payload_text = json.dumps(captured_spans)
        assert "TOOL_NO_GEN_SECRET" not in payload_text
        assert "TOOL_NO_GEN_OUTPUT" not in payload_text
        assert "TOOL_NO_GEN_FAILURE" not in payload_text
        assert "SUBAGENT_NO_GEN_SECRET" not in payload_text
        assert "SUBAGENT_NO_GEN_OUTPUT" not in payload_text

    def test_no_gen_id_skips_cleanup(self, captured_spans, monkeypatch):
        """Without gen_id, state_cleanup_generation is not called."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation") as cleanup,
        ):
            _dispatch("stop", {"conversation_id": "c1"})

        cleanup.assert_not_called()
        assert len(captured_spans) == 1

    def test_optional_attrs_omitted(self, captured_spans, monkeypatch):
        """Status and loop_count omitted when empty."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch("stop", {"conversation_id": "c1", "generation_id": "g1"})

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "cursor.stop.status" not in attr_keys
        assert "cursor.stop.loop_count" not in attr_keys

    @pytest.mark.parametrize("event", ["stop", "sessionEnd"])
    def test_terminal_claim_failure_retries_without_resending(self, monkeypatch, event):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch(
                "tracing.cursor.hooks.handlers.generation_claim_terminal_event",
                side_effect=[sqlite3.OperationalError("database is locked"), True],
            ) as mark,
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None) as pop,
            mock.patch("tracing.cursor.hooks.handlers.send_span", return_value=True) as send,
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation") as cleanup,
        ):
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                _dispatch(event, {"conversation_id": "c1", "generation_id": "g1"})
            _dispatch(event, {"conversation_id": "c1", "generation_id": "g1"})

        assert mark.call_count == 2
        mark.assert_called_with("g1", event)
        pop.assert_not_called()
        send.assert_called_once()
        cleanup.assert_called_once_with("g1")

    @pytest.mark.parametrize("event", ["stop", "sessionEnd"])
    def test_terminal_transport_exception_retries_before_cleanup(self, monkeypatch, event):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
            mock.patch(
                "tracing.cursor.hooks.handlers.send_span",
                side_effect=[RuntimeError("send failed"), True],
            ) as send,
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation") as cleanup,
        ):
            with pytest.raises(RuntimeError, match="send failed"):
                _dispatch(event, {"conversation_id": "c1", "generation_id": "g1"})
            cleanup.assert_not_called()
            assert adapter.generation_is_completed("g1") is False
            _dispatch(event, {"conversation_id": "c1", "generation_id": "g1"})

        assert send.call_count == 2
        cleanup.assert_called_once_with("g1")
        assert adapter.generation_is_completed("g1") is True

    @pytest.mark.parametrize("event", ["stop", "sessionEnd"])
    def test_later_nonterminal_event_retries_failed_terminal_cleanup(self, monkeypatch, event):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        payload = {"conversation_id": "c1", "generation_id": "g1"}
        adapter.gen_root_span_save("g1", "private-root")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
            mock.patch("tracing.cursor.hooks.handlers.send_span") as send,
            mock.patch(
                "tracing.cursor.hooks.handlers.state_cleanup_generation",
                side_effect=OSError("unlink failed"),
            ) as cleanup,
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation_digest") as swept_cleanup,
        ):
            with pytest.raises(OSError, match="unlink failed"):
                _dispatch(event, payload)
            _dispatch("workspaceOpen", payload)

        cleanup.assert_called_once_with("g1")
        swept_cleanup.assert_called_once_with(adapter.stable_digest("g1"))
        assert send.call_count == 1
        assert adapter.generation_is_completed("g1") is True

    def test_real_main_sweeps_pending_cleanup_when_tracing_disabled(self, monkeypatch, capfd):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        adapter.gen_root_span_save("g1", "DISABLED_PRIVATE_NEEDLE")
        adapter.generation_mark_completed("g1", cleanup_pending=True)
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

        main()

        assert adapter.gen_root_span_get("g1") == ""
        assert adapter.generation_pending_cleanup_batch() == ([], False)
        assert json.loads(capfd.readouterr().out) == {}

    def test_malformed_pending_digest_suppresses_dispatch(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        adapter.generation_mark_completed("completed", cleanup_pending=True)
        with sqlite3.connect(adapter._completed_db_path()) as connection:
            connection.execute("PRAGMA ignore_check_constraints = ON")
            connection.execute("DROP TRIGGER completed_digest_insert_guard")
            connection.execute(
                "INSERT INTO completed_generations(digest) VALUES (?)",
                ("z" * 64,),
            )
            connection.execute("UPDATE completion_metadata SET row_count = row_count + 1")
        with mock.patch("tracing.cursor.hooks.handlers._handle_workspace_open") as handler:
            _dispatch("workspaceOpen", {"generation_id": "active"})
        handler.assert_not_called()

    def test_pending_cleanup_never_uses_glob_or_path_iterdir(self):
        gen_id = "no-directory-walk"
        digest = adapter.stable_digest(gen_id)
        shard = adapter.STATE_DIR / adapter.generation_state_key(gen_id)
        shard.mkdir(parents=True)
        (shard / "private.stack.json").write_text("private")
        adapter.generation_mark_completed(gen_id, cleanup_pending=True)

        with (
            mock.patch("pathlib.Path.glob", side_effect=AssertionError("glob is unbounded")),
            mock.patch("pathlib.Path.iterdir", side_effect=AssertionError("iterdir materializes the directory")),
        ):
            assert _sweep_pending_generation_cleanups() is True

        assert not shard.exists()
        assert adapter.generation_pending_cleanup_batch() == ([], False)
        assert digest not in str(list(adapter.STATE_DIR.rglob("*")))

    def test_pending_cleanup_filesystem_work_is_bounded_and_progresses(self):
        gen_id = "many-private-files"
        digest = adapter.stable_digest(gen_id)
        token = adapter.generation_state_key(gen_id)
        shard = adapter.STATE_DIR / token
        shard.mkdir(parents=True)
        file_count = 2 * adapter.STATE_CLEANUP_ENTRY_LIMIT + 1
        for index in range(file_count):
            (shard / f"private-{index}.stack.json").write_text("private")
        adapter.generation_mark_completed(gen_id, cleanup_pending=True)

        before = len(list(shard.iterdir()))
        assert _sweep_pending_generation_cleanups() is False
        after = len(list(shard.iterdir()))
        assert 0 < before - after <= adapter.STATE_CLEANUP_ENTRY_LIMIT
        assert adapter.generation_pending_cleanup_batch() == ([digest], False)

        for _ in range(file_count + 1):
            if _sweep_pending_generation_cleanups():
                break
        assert not shard.exists()
        assert adapter.generation_pending_cleanup_batch() == ([], False)

    def test_nonempty_lock_directory_keeps_pending_cleanup_durable(self):
        gen_id = "gen-lockdir-pending"
        digest = adapter.stable_digest(gen_id)
        safe = adapter.generation_state_key(gen_id)
        lock_dir = adapter.STATE_DIR / safe / f".lock_before_{safe}_shell"
        lock_dir.mkdir(parents=True)
        (lock_dir / "stale").write_text("private")
        adapter.generation_mark_completed(gen_id, cleanup_pending=True)

        assert _sweep_pending_generation_cleanups() is False
        assert lock_dir.exists()
        assert adapter.generation_pending_cleanup_batch() == ([digest], False)

    def test_pending_sweep_uses_one_bounded_bulk_ledger_update(self):
        digests = [f"{value:064x}" for value in range(adapter.PENDING_CLEANUP_BATCH_SIZE)]
        with (
            mock.patch(
                "tracing.cursor.hooks.handlers.generation_pending_cleanup_batch",
                return_value=(digests, True),
            ),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation_digest") as cleanup,
            mock.patch("tracing.cursor.hooks.handlers.generation_finish_pending_cleanup_batch") as finish,
        ):
            assert _sweep_pending_generation_cleanups() is False
        assert cleanup.call_count == len(digests)
        finish.assert_called_once_with(digests, [])

    def test_pending_cleanup_batch_is_bounded_and_preserves_remainder(self):
        adapter.generation_mark_completed("seed")
        digests = [f"{value:064x}" for value in range(adapter.PENDING_CLEANUP_BATCH_SIZE + 1)]
        with sqlite3.connect(adapter._completed_db_path()) as connection:
            connection.executemany(
                "INSERT INTO completed_generations(digest) VALUES (?)",
                ((digest,) for digest in digests),
            )
            connection.executemany(
                "INSERT INTO pending_generation_cleanups(digest) VALUES (?)",
                ((digest,) for digest in digests),
            )
            connection.execute(
                "UPDATE completion_metadata SET row_count = row_count + ?",
                (len(digests),),
            )

        first, has_more = adapter.generation_pending_cleanup_batch()
        assert len(first) == adapter.PENDING_CLEANUP_BATCH_SIZE
        assert has_more is True
        adapter.generation_finish_pending_cleanup_batch(first, [])
        assert adapter.generation_pending_cleanup_batch() == ([digests[-1]], False)

    def test_durable_state_files_are_private_even_with_open_umask(self):
        previous_umask = os.umask(0)
        try:
            adapter.state_push("private-stack", {"secret": "value"})
            adapter.gen_root_span_save("private-generation", "private-span")
            with adapter.generation_guard("private-generation"):
                pass
            adapter.generation_mark_completed("private-generation")
        finally:
            os.umask(previous_umask)

        for path in adapter.STATE_DIR.rglob("*"):
            expected_mode = 0o700 if path.is_dir() else 0o600
            assert path.stat().st_mode & 0o777 == expected_mode, path

    def test_main_sweeps_once_before_dispatch(self, monkeypatch, capfd):
        monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
        with (
            mock.patch("tracing.cursor.hooks.handlers._sweep_pending_generation_cleanups", return_value=True) as sweep,
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch") as dispatch,
        ):
            main()
        sweep.assert_called_once_with()
        dispatch.assert_called_once_with("", {}, sweep_pending=False)
        assert json.loads(capfd.readouterr().out) == {}


# ---------------------------------------------------------------------------
# _handle_before_shell_execution tests
# ---------------------------------------------------------------------------


class TestHandleBeforeShellExecution:

    def test_pushes_state(self, monkeypatch):
        """Pushes command, cwd, start_ms, trace_id, conversation_id to state."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.state_push") as push_mock,
        ):
            _dispatch(
                "beforeShellExecution",
                {
                    "conversation_id": "c1",
                    "generation_id": "gen-1",
                    "command": "ls -la",
                    "cwd": "/home",
                },
            )

        push_mock.assert_called_once()
        key, value = push_mock.call_args[0]
        assert key == f"shell_{adapter.generation_state_key('gen-1')}"
        assert value["command"] == "ls -la"
        assert value["cwd"] == "/home"
        assert value["start_ms"] == "1000"

    def test_no_gen_id_returns_early(self, monkeypatch):
        """Without gen_id, returns without pushing state."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.state_push") as push_mock,
        ):
            _dispatch(
                "beforeShellExecution",
                {
                    "conversation_id": "c1",
                    "command": "ls",
                },
            )

        push_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_after_agent_thought tests
# ---------------------------------------------------------------------------


class TestHandleAfterAgentThought:

    def test_creates_chain_span_with_thought(self, captured_spans, monkeypatch):
        """Creates CHAIN span with thought as output.value."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="abcd" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent1") as get_mock,
        ):
            _dispatch(
                "afterAgentThought",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "thought": "thinking about the problem",
                },
            )

        get_mock.assert_called_once_with("gen-1")
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["output.value"]["stringValue"] == "thinking about the problem"
        assert attrs["session.id"]["stringValue"] == "conv-1"
        assert span["name"] == "Agent Thinking"
        assert span["parentSpanId"] == "parent1"


# ---------------------------------------------------------------------------
# _handle_before_mcp_execution tests
# ---------------------------------------------------------------------------


class TestHandleBeforeMcpExecution:

    def test_pushes_state(self, monkeypatch):
        """Pushes tool_name, tool_input, url, command, start_ms to state."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1500),
            mock.patch("tracing.cursor.hooks.handlers.state_push") as push_mock,
        ):
            _dispatch(
                "beforeMCPExecution",
                {
                    "conversation_id": "c1",
                    "generation_id": "gen-1",
                    "tool_name": "search",
                    "tool_input": '{"query": "test"}',
                    "url": "http://localhost:3000",
                },
            )

        push_mock.assert_called_once()
        key, value = push_mock.call_args[0]
        assert key == f"mcp_{adapter.generation_state_key('gen-1')}"
        assert value["tool_name"] == "search"
        assert value["tool_input"] == '{"query": "test"}'
        assert value["start_ms"] == "1500"

    def test_no_gen_id_returns_early(self, monkeypatch):
        """Without gen_id, returns without pushing state."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.state_push") as push_mock,
        ):
            _dispatch(
                "beforeMCPExecution",
                {
                    "conversation_id": "c1",
                    "tool_name": "search",
                },
            )

        push_mock.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_after_mcp_execution tests
# ---------------------------------------------------------------------------


class TestHandleAfterMcpExecution:

    def test_creates_tool_span_with_popped_state(self, captured_spans, monkeypatch):
        """Creates TOOL span, merges with before state from state_pop."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        popped = {
            "tool_name": "search",
            "tool_input": '{"query": "test"}',
            "url": "http://localhost:3000",
            "command": "",
            "start_ms": "1000",
            "trace_id": "t1",
            "conversation_id": "c1",
        }
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="ffaa" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent1"),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=popped),
        ):
            _dispatch(
                "afterMCPExecution",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "result": "found 3 items",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "search"
        assert attrs["input.value"]["stringValue"] == '{"query": "test"}'
        assert attrs["output.value"]["stringValue"] == "found 3 items"
        assert span["name"] == "MCP: search"
        assert span["parentSpanId"] == "parent1"

    def test_no_popped_state_uses_input(self, captured_spans, monkeypatch):
        """Without popped state, span still created from input_json fields."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="bbcc" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
        ):
            _dispatch(
                "afterMCPExecution",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "tool_name": "list_repos",
                    "result": "ok",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["tool.name"]["stringValue"] == "list_repos"
        assert span["name"] == "MCP: list_repos"


# ---------------------------------------------------------------------------
# _handle_before_read_file tests
# ---------------------------------------------------------------------------


class TestHandleBeforeReadFile:

    def test_creates_tool_span(self, captured_spans, monkeypatch):
        """Creates TOOL span with file path as input."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="1122" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent1"),
        ):
            _dispatch(
                "beforeReadFile",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "file_path": "/foo/bar.py",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "read_file"
        assert attrs["input.value"]["stringValue"] == "/foo/bar.py"
        assert span["name"] == "Read File"
        assert span["parentSpanId"] == "parent1"


# ---------------------------------------------------------------------------
# _handle_after_file_edit tests
# ---------------------------------------------------------------------------


class TestHandleAfterFileEdit:

    def test_creates_tool_span(self, captured_spans, monkeypatch):
        """Creates TOOL span with file path and diff."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="3344" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent1"),
        ):
            _dispatch(
                "afterFileEdit",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "file_path": "/foo/bar.py",
                    "diff": "+added line",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "edit_file"
        assert attrs["input.value"]["stringValue"] == "/foo/bar.py: +added line"
        assert span["name"] == "File Edit"
        assert span["parentSpanId"] == "parent1"

    def test_no_diff_uses_path_only(self, captured_spans, monkeypatch):
        """Without diff, input.value is just the file path."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="3344" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(
                "afterFileEdit",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "file_path": "/foo/bar.py",
                },
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["input.value"]["stringValue"] == "/foo/bar.py"


# ---------------------------------------------------------------------------
# _handle_before_tab_file_read tests
# ---------------------------------------------------------------------------


class TestHandleBeforeTabFileRead:

    def test_creates_tool_span(self, captured_spans, monkeypatch):
        """Creates TOOL span with file path as input for tab read."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="5566" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent1"),
        ):
            _dispatch(
                "beforeTabFileRead",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "file_path": "/src/main.ts",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "read_file_tab"
        assert attrs["input.value"]["stringValue"] == "/src/main.ts"
        assert span["name"] == "Tab Read File"
        assert span["parentSpanId"] == "parent1"


# ---------------------------------------------------------------------------
# _handle_after_tab_file_edit tests
# ---------------------------------------------------------------------------


class TestHandleAfterTabFileEdit:

    def test_creates_tool_span(self, captured_spans, monkeypatch):
        """Creates TOOL span with file path and edits for tab edit."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="7788" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent1"),
        ):
            _dispatch(
                "afterTabFileEdit",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "file_path": "/src/main.ts",
                    "edits": "replaced function",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "edit_file_tab"
        assert attrs["input.value"]["stringValue"] == "/src/main.ts: replaced function"
        assert span["name"] == "Tab File Edit"
        assert span["parentSpanId"] == "parent1"

    def test_no_edits_uses_path_only(self, captured_spans, monkeypatch):
        """Without edits, input.value is just the file path."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="7788" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(
                "afterTabFileEdit",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "file_path": "/src/main.ts",
                },
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["input.value"]["stringValue"] == "/src/main.ts"


# ---------------------------------------------------------------------------
# main() entry point tests
# ---------------------------------------------------------------------------


class TestMain:

    def test_reads_stdin_dispatches_prints_permissive(self, monkeypatch, tmp_path):
        """main() reads JSON from stdin, dispatches, prints permissive response."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_FILE", str(tmp_path / "hook.log"))

        input_data = {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "c1",
            "generation_id": "g1",
            "prompt": "hello",
        }
        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(input_data))),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch") as dispatch_mock,
        ):
            main()

        dispatch_mock.assert_called_once_with("beforeSubmitPrompt", input_data, sweep_pending=False)
        result = json.loads(stdout_buf.getvalue())
        assert result == {"continue": True}

    def test_invalid_json_still_prints_permissive(self, monkeypatch, tmp_path):
        """Invalid JSON on stdin still prints permissive response."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_FILE", str(tmp_path / "hook.log"))

        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO("not valid json")),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
        ):
            main()

        result = json.loads(stdout_buf.getvalue())
        # No event-specific response can be selected after malformed input.
        assert result == {}

    def test_exception_in_dispatch_still_prints_permissive(self, monkeypatch, tmp_path):
        """Exception in _dispatch still prints permissive response."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_FILE", str(tmp_path / "hook.log"))

        input_data = {
            "hook_event_name": "afterAgentResponse",
            "conversation_id": "c1",
            "generation_id": "g1",
        }
        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(input_data))),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch", side_effect=RuntimeError("boom")),
        ):
            main()

        result = json.loads(stdout_buf.getvalue())
        assert result == {}

    def test_check_requirements_false_still_prints_permissive(self, monkeypatch, tmp_path):
        """When check_requirements returns False, still prints permissive."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "false")
        monkeypatch.setenv("ARIZE_LOG_FILE", str(tmp_path / "hook.log"))

        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO('{"hook_event_name":"beforeSubmitPrompt"}')),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=False),
        ):
            main()

        result = json.loads(stdout_buf.getvalue())
        # event is "" because we return before reading stdin
        assert result == {}

    def test_empty_stdin(self, monkeypatch, tmp_path):
        """Empty stdin produces empty dict, still prints permissive."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_FILE", str(tmp_path / "hook.log"))

        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO("")),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch") as dispatch_mock,
        ):
            main()

        dispatch_mock.assert_called_once_with("", {}, sweep_pending=False)
        result = json.loads(stdout_buf.getvalue())
        assert result == {}

    def test_stderr_redirected_to_log_file(self, monkeypatch, tmp_path):
        """main() redirects stderr to env.log_file."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        log_file = tmp_path / "hook.log"
        monkeypatch.setenv("ARIZE_LOG_FILE", str(log_file))

        stdout_buf = io.StringIO()
        original_stderr = sys.stderr

        with (
            mock.patch("sys.stdin", io.StringIO('{"hook_event_name":"stop"}')),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch"),
        ):
            main()

        # Restore stderr for safety
        sys.stderr = original_stderr


# ---------------------------------------------------------------------------
# _event_name tests
# ---------------------------------------------------------------------------


class TestEventName:

    def test_supports_hook_event_name(self):
        assert _event_name({"hook_event_name": "stop"}) == "stop"

    def test_supports_hookEventName(self):
        assert _event_name({"hookEventName": "sessionStart"}) == "sessionStart"

    def test_supports_event_name(self):
        assert _event_name({"event_name": "postToolUse"}) == "postToolUse"

    def test_supports_eventName(self):
        assert _event_name({"eventName": "afterFileEdit"}) == "afterFileEdit"

    def test_supports_event(self):
        assert _event_name({"event": "beforeSubmitPrompt"}) == "beforeSubmitPrompt"

    def test_prefers_hook_event_name_over_hookEventName(self):
        assert _event_name({"hook_event_name": "stop", "hookEventName": "sessionStart"}) == "stop"

    def test_returns_empty_for_missing(self):
        assert _event_name({}) == ""


# ---------------------------------------------------------------------------
# _trace_id_from_event tests
# ---------------------------------------------------------------------------


class TestTraceIdFromEvent:

    def test_prefers_gen_id(self):
        result = _trace_id_from_event("gen-1", "conv-1")
        assert result  # non-empty
        from tracing.cursor.hooks.adapter import trace_id_from_generation

        assert result == trace_id_from_generation("gen-1")

    def test_falls_back_to_conversation_id(self):
        result = _trace_id_from_event("", "conv-1")
        assert result
        from tracing.cursor.hooks.adapter import trace_id_from_generation

        assert result == trace_id_from_generation("conv-1")

    def test_returns_empty_when_both_empty(self):
        assert _trace_id_from_event("", "") == ""


# ---------------------------------------------------------------------------
# _dispatch routes sessionStart / postToolUse
# ---------------------------------------------------------------------------


class TestDispatchNewEvents:

    def test_dispatch_routes_session_start(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_session_start") as h,
        ):
            _dispatch(
                "sessionStart",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                },
            )
            h.assert_called_once()

    def test_dispatch_routes_session_end(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_session_end") as h,
        ):
            _dispatch(
                "sessionEnd",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                },
            )
            h.assert_called_once()

    def test_dispatch_routes_post_tool_use(self, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers._handle_post_tool_use") as h,
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                },
            )
            h.assert_called_once()


# ---------------------------------------------------------------------------
# main() dispatches camelCase event key
# ---------------------------------------------------------------------------


class TestMainCamelCase:

    def test_main_dispatches_camel_case_event_key(self, monkeypatch, tmp_path):
        """main() resolves hookEventName from CLI payloads and dispatches correctly."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_FILE", str(tmp_path / "hook.log"))

        input_data = {
            "hookEventName": "sessionStart",
            "conversation_id": "c1",
            "generation_id": "g1",
            "cwd": "/tmp",
        }
        stdout_buf = io.StringIO()

        with (
            mock.patch("sys.stdin", io.StringIO(json.dumps(input_data))),
            mock.patch.object(sys, "__stdout__", stdout_buf),
            mock.patch("tracing.cursor.hooks.handlers.check_requirements", return_value=True),
            mock.patch("tracing.cursor.hooks.handlers._dispatch") as dispatch_mock,
        ):
            main()

        dispatch_mock.assert_called_once_with("sessionStart", input_data, sweep_pending=False)


# ---------------------------------------------------------------------------
# _handle_session_start tests
# ---------------------------------------------------------------------------


class TestHandleSessionStart:

    def test_session_start_sends_chain_span(self, captured_spans, monkeypatch):
        """sessionStart produces a CHAIN span with session.id and cwd."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="ss11" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_save") as save_mock,
        ):
            _dispatch(
                "sessionStart",
                {
                    "conversation_id": "conv-sess",
                    "generation_id": "gen-sess",
                    "cwd": "/Users/alice/code/myrepo",
                    "user_email": "alice@example.com",
                },
            )

        save_mock.assert_called_once_with("gen-sess", "ss11" * 4)
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert span["name"] == "Session Start"
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["session.id"]["stringValue"] == "conv-sess"
        assert attrs["cursor.session.cwd"]["stringValue"] == "/Users/alice/code/myrepo"

    def test_session_start_no_gen_id_skips_save(self, captured_spans, monkeypatch):
        """Without gen_id, gen_root_span_save is not called."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_save") as save_mock,
        ):
            _dispatch(
                "sessionStart",
                {
                    "conversation_id": "conv-sess",
                    "cwd": "/tmp",
                },
            )

        save_mock.assert_not_called()
        assert len(captured_spans) == 1

    def test_session_start_optional_fields_omitted(self, captured_spans, monkeypatch):
        """Optional fields like cwd and user_email are omitted when absent."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),):
            _dispatch(
                "sessionStart",
                {
                    "conversation_id": "conv-sess",
                },
            )

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "cursor.session.cwd" not in attr_keys


# ---------------------------------------------------------------------------
# _handle_post_tool_use tests
# ---------------------------------------------------------------------------


class TestHandlePostToolUse:

    def test_post_tool_use_sends_tool_span(self, captured_spans, monkeypatch):
        """postToolUse produces a TOOL span with tool.name, input.value, output.value."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.span_id_16", return_value="pt11" * 4),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="parent-pt"),
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "conv-pt",
                    "generation_id": "gen-pt",
                    "toolName": "code_search",
                    "toolInput": '{"query": "main function"}',
                    "result": "<search results>",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert span["name"] == "Tool: code_search"
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "code_search"
        assert attrs["input.value"]["stringValue"] == '{"query": "main function"}'
        assert attrs["output.value"]["stringValue"] == "<search results>"
        assert attrs["session.id"]["stringValue"] == "conv-pt"
        assert span["parentSpanId"] == "parent-pt"

    def test_post_tool_use_unknown_tool_uses_command_field_when_present(self, captured_spans, monkeypatch):
        """Non-deduped shell-like tool uses 'command' field as input.value fallback."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "toolName": "custom_runner",
                    "command": "ls -la",
                    "stdout": "total 40\ndrwxr-xr-x ...",
                },
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        # custom_runner is not in _SHELL_TOOL_NAMES so command field is NOT used as input
        # tool_input comes from the standard extraction keys
        assert attrs["output.value"]["stringValue"] == "total 40\ndrwxr-xr-x ..."

    def test_post_tool_use_missing_fields_omitted(self, captured_spans, monkeypatch):
        """Missing optional fields are omitted from attributes."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                },
            )

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "tool.name" not in attr_keys
        assert "input.value" not in attr_keys
        assert "output.value" not in attr_keys

    def test_post_tool_use_skips_shell_tool(self, captured_spans, monkeypatch):
        """postToolUse with tool_name='shell' is skipped (covered by dedicated handler)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "toolName": "shell",
                    "command": "ls -la",
                },
            )

        assert len(captured_spans) == 0

    @pytest.mark.parametrize(
        "tool_name",
        ["shell", "Shell", "TERMINAL", "bash", "read_file", "edit_file", "tab_file_read", "mcp"],
    )
    def test_post_tool_use_skips_each_dedicated_tool_name(self, captured_spans, monkeypatch, tool_name):
        """postToolUse short-circuits for each known dedicated tool name (case-insensitive)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "toolName": tool_name,
                },
            )

        assert len(captured_spans) == 0, f"Expected no span for tool_name={tool_name!r}"

    def test_post_tool_use_mcp_dedup_matrix(self, captured_spans, monkeypatch):
        """Delivery-aware MCP de-duplication: generic spans are suppressed only
        when the dedicated pair demonstrably emitted the span (real hosts name
        MCP calls "MCP:<tool>" in generic events); a generic-only surface must
        keep its telemetry, and failure paths must not duplicate either."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        # Real payload shapes (Cursor 2026.07.16): the generic events carry the
        # arguments as an object, the dedicated pair the same arguments as a
        # JSON string, and only the generic events carry a tool_use_id.
        common = {
            "conversation_id": "c1",
            "generation_id": "g1",
            "tool_name": "MCP:echo_marker",
            "tool_input": {"text": "idcheck"},
        }
        mcp_common = {
            "conversation_id": "c1",
            "generation_id": "g1",
            "tool_name": "echo_marker",
            "tool_input": '{"text":"idcheck"}',
        }
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            # 1. generic-only success → generic span survives
            _dispatch("preToolUse", {**common, "tool_use_id": "m1"})
            _dispatch("postToolUse", {**common, "tool_use_id": "m1", "tool_output": "ok"})
            # 2. dedicated + generic success → only the dedicated span
            _dispatch("beforeMCPExecution", {**mcp_common})
            _dispatch("preToolUse", {**common, "tool_use_id": "m2"})
            _dispatch("afterMCPExecution", {**mcp_common, "result": "echoed"})
            _dispatch("postToolUse", {**common, "tool_use_id": "m2", "tool_output": "echoed"})
            # 3. generic-only failure → generic error span survives
            _dispatch("preToolUse", {**common, "tool_use_id": "m3"})
            _dispatch("postToolUseFailure", {**common, "tool_use_id": "m3", "error_message": "boom"})
            # 4. dedicated + generic failure → only the dedicated span, as error
            _dispatch("beforeMCPExecution", {**mcp_common})
            _dispatch("preToolUse", {**common, "tool_use_id": "m4"})
            _dispatch("afterMCPExecution", {**mcp_common, "error": "deliberate failure"})
            _dispatch("postToolUseFailure", {**common, "tool_use_id": "m4", "error_message": "deliberate failure"})
            # 5. denied call (observed CLI shape: rejection in the result)
            _dispatch("beforeMCPExecution", {**mcp_common})
            _dispatch("preToolUse", {**common, "tool_use_id": "m5"})
            rejection = '{"rejected":true,"reason":"User rejected"}'
            _dispatch("afterMCPExecution", {**mcp_common, "result": rejection})
            _dispatch("postToolUse", {**common, "tool_use_id": "m5", "tool_output": rejection})

        spans = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for s in captured_spans]
        names = [s["name"] for s in spans]
        assert names == [
            "Tool: MCP:echo_marker",  # 1: generic-only success
            "MCP: echo_marker",  # 2: dedicated wins
            "Tool: MCP:echo_marker",  # 3: generic-only failure
            "MCP: echo_marker",  # 4: dedicated failure wins
            "MCP: echo_marker",  # 5: denied
        ]
        # 3: generic-only failure carries OTLP ERROR
        assert spans[2]["status"] == {"code": 2, "message": "boom"}
        # 4: the surviving dedicated failure span carries the error itself
        assert spans[3]["status"] == {"code": 2, "message": "deliberate failure"}
        failure_attrs = _attrs(spans[3])
        assert failure_attrs["cursor.tool.status"]["stringValue"] == "error"
        # 5: rejection reason survives on the dedicated span
        denied_attrs = _attrs(spans[4])
        assert "rejected" in denied_attrs["output.value"]["stringValue"]

    def test_mcp_dedup_does_not_suppress_a_different_invocation(self, captured_spans, monkeypatch):
        """A dedicated call whose generic follow-up never arrives must not
        suppress a later, separate generic-only call of the same tool."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        # Real payload shapes (Cursor 2026.07.16): the generic events carry the
        # arguments as an object, the dedicated pair the same arguments as a
        # JSON string, and only the generic events carry a tool_use_id.
        common = {
            "conversation_id": "c1",
            "generation_id": "g1",
            "tool_name": "MCP:echo_marker",
            "tool_input": {"text": "idcheck"},
        }
        mcp_common = {
            "conversation_id": "c1",
            "generation_id": "g1",
            "tool_name": "echo_marker",
            "tool_input": '{"text":"idcheck"}',
        }
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            # Call A: dedicated pair only — its generic follow-up never lands.
            _dispatch("beforeMCPExecution", {**mcp_common})
            _dispatch("afterMCPExecution", {**mcp_common, "result": "first"})
            # Call B: a genuinely separate invocation, generic events only.
            _dispatch("preToolUse", {**common, "tool_use_id": "generic-2"})
            _dispatch("postToolUse", {**common, "tool_use_id": "generic-2", "tool_output": "second"})

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["MCP: echo_marker", "Tool: MCP:echo_marker"]

    @pytest.mark.parametrize("completion_event", ["postToolUse", "postToolUseFailure"])
    def test_duplicate_generic_completion_emits_once(self, captured_spans, monkeypatch, completion_event):
        """Retried generic completion of an invocation already reported by the
        dedicated MCP span must not produce a second span."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        # Real payload shapes (Cursor 2026.07.16): the generic events carry the
        # arguments as an object, the dedicated pair the same arguments as a
        # JSON string, and only the generic events carry a tool_use_id.
        common = {
            "conversation_id": "c1",
            "generation_id": "g1",
            "tool_name": "MCP:echo_marker",
            "tool_input": {"text": "idcheck"},
        }
        mcp_common = {
            "conversation_id": "c1",
            "generation_id": "g1",
            "tool_name": "echo_marker",
            "tool_input": '{"text":"idcheck"}',
        }
        completion = {**common, "tool_use_id": "generic-1"}
        if completion_event == "postToolUse":
            completion["tool_output"] = "ok"
            dedicated_outcome = {"result": "ok"}
        else:
            completion["error_message"] = "boom"
            dedicated_outcome = {"error": "boom"}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch("beforeMCPExecution", {**mcp_common})
            _dispatch("preToolUse", {**common, "tool_use_id": "generic-1"})
            _dispatch("afterMCPExecution", {**mcp_common, **dedicated_outcome})
            _dispatch(completion_event, completion)
            _dispatch(completion_event, completion)  # duplicate delivery

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["MCP: echo_marker"]

    def test_interleaved_distinct_mcp_calls_each_keep_a_span(self, captured_spans, monkeypatch):
        """A dedicated pair for one call must never claim a different call.

        Delivery can interleave two operations (here the dedicated pair for B
        lands inside A's generic invocation). Correlation is by call content,
        so B's result cannot consume A's invocation and both stay observable.
        """
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch("preToolUse", {**gen, "tool_name": "MCP:alpha", "tool_input": {"a": 1}, "tool_use_id": "A"})
            _dispatch("beforeMCPExecution", {**gen, "tool_name": "beta", "tool_input": '{"b":2}'})
            _dispatch("afterMCPExecution", {**gen, "tool_name": "beta", "tool_input": '{"b":2}', "result": "B"})
            _dispatch(
                "postToolUse",
                {**gen, "tool_name": "MCP:alpha", "tool_input": {"a": 1}, "tool_use_id": "A", "tool_output": "A"},
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["MCP: beta", "Tool: MCP:alpha"]

    @pytest.mark.parametrize("outcome", ["success", "failure"])
    def test_failed_dedicated_export_leaves_the_generic_fallback(self, monkeypatch, outcome):
        """A dedicated span the backend refused must not suppress anything.

        The marker asserts a span exists; a send that reported failure did not
        create one, so the generic completion has to remain available as the
        fallback it naturally is.
        """
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        dedicated = {**gen, "tool_name": "echo", "tool_input": '{"text":"t"}'}
        generic = {**gen, "tool_name": "MCP:echo", "tool_input": {"text": "t"}, "tool_use_id": "A"}
        if outcome == "success":
            dedicated_end, completion_event = {"result": "same"}, "postToolUse"
            completion = {**generic, "tool_output": "same"}
            expected_status = {"code": 1}
        else:
            dedicated_end, completion_event = {"error": "same"}, "postToolUseFailure"
            completion = {**generic, "error_message": "same"}
            expected_status = {"code": 2, "message": "same"}

        attempts = []

        def failing_dedicated_send(payload):
            name = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"]
            attempts.append(payload)
            return not name.startswith("MCP: ")

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.send_span", side_effect=failing_dedicated_send),
        ):
            _dispatch("beforeMCPExecution", dedicated)
            _dispatch("afterMCPExecution", {**dedicated, **dedicated_end})
            _dispatch(completion_event, completion)

        names = [payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for payload in attempts]
        assert names == ["MCP: echo", "Tool: MCP:echo"], "the generic completion must still reach the backend"
        assert attempts[1]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["status"] == expected_status
        # The refused export left no marker behind to suppress a later call.
        assert not [path for path in adapter.STATE_DIR.rglob("mcpdone_*") if path.is_file()]

    def test_unmatched_after_event_does_not_steal_another_calls_state(self, captured_spans, monkeypatch):
        """An after-event whose before-event was lost must not take a record
        belonging to a different call.

        The payload identifies its own call, so a missing match is evidence
        the record is absent — not licence to pop whatever is on the stack.
        """
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        alpha = {**gen, "tool_name": "alpha", "tool_input": '{"call":"A"}'}
        beta = {**gen, "tool_name": "beta", "tool_input": '{"call":"B"}'}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", side_effect=itertools.count(1000, 10)):
            _dispatch("beforeMCPExecution", beta)  # alpha's before-event is lost
            _dispatch("afterMCPExecution", {**alpha, "result": "result-A"})
            _dispatch("afterMCPExecution", {**beta, "result": "result-B"})

        spans = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for s in captured_spans]
        by_name = {span["name"]: _attrs(span) for span in spans}
        assert set(by_name) == {"MCP: alpha", "MCP: beta"}
        # alpha falls back to its own payload's arguments, not beta's record.
        assert by_name["MCP: alpha"]["input.value"]["stringValue"] == '{"call":"A"}'
        assert by_name["MCP: alpha"]["output.value"]["stringValue"] == "result-A"
        # beta's record survived the unmatched after-event and is still its own.
        assert by_name["MCP: beta"]["input.value"]["stringValue"] == '{"call":"B"}'
        assert by_name["MCP: beta"]["output.value"]["stringValue"] == "result-B"

    def test_dedicated_success_does_not_hide_a_generic_failure(self, captured_spans, monkeypatch):
        """Same text, different outcome, different call — both must be kept."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        dedicated = {**gen, "tool_name": "echo", "tool_input": '{"text":"t"}'}
        generic = {**gen, "tool_name": "MCP:echo", "tool_input": {"text": "t"}, "tool_use_id": "B"}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch("beforeMCPExecution", dedicated)
            _dispatch("afterMCPExecution", {**dedicated, "result": "same"})
            _dispatch("postToolUseFailure", {**generic, "error_message": "same"})

        spans = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for s in captured_spans]
        assert [span["name"] for span in spans] == ["MCP: echo", "Tool: MCP:echo"]
        assert spans[0]["status"] == {"code": 1}
        assert spans[1]["status"] == {"code": 2, "message": "same"}

    def test_dedicated_failure_does_not_hide_a_generic_success(self, captured_spans, monkeypatch):
        """The mirror case: a failed call must not absorb a successful one."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        dedicated = {**gen, "tool_name": "echo", "tool_input": '{"text":"t"}'}
        generic = {**gen, "tool_name": "MCP:echo", "tool_input": {"text": "t"}, "tool_use_id": "B"}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch("beforeMCPExecution", dedicated)
            _dispatch("afterMCPExecution", {**dedicated, "error": "same"})
            _dispatch("postToolUse", {**generic, "tool_output": "same"})

        spans = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for s in captured_spans]
        assert [span["name"] for span in spans] == ["MCP: echo", "Tool: MCP:echo"]
        assert spans[0]["status"] == {"code": 2, "message": "same"}
        assert spans[1]["status"] == {"code": 1}

    def test_concurrent_identical_calls_get_one_span_each(self, captured_spans, monkeypatch):
        """Two identical calls in flight at once yield one span per call.

        Nothing distinguishes the invocations, but each dedicated result is
        recorded once and each generic completion consumes one record, so the
        pairing is one-for-one however the events interleave.
        """
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        generic = {**gen, "tool_name": "MCP:echo", "tool_input": {"text": "same"}}
        dedicated = {**gen, "tool_name": "echo", "tool_input": '{"text":"same"}'}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch("preToolUse", {**generic, "tool_use_id": "one"})
            _dispatch("preToolUse", {**generic, "tool_use_id": "two"})
            _dispatch("beforeMCPExecution", dedicated)
            _dispatch("beforeMCPExecution", dedicated)
            _dispatch("afterMCPExecution", {**dedicated, "result": "r"})
            _dispatch("afterMCPExecution", {**dedicated, "result": "r"})
            # Real hosts have been observed completing these in the opposite
            # order to their preToolUse events.
            _dispatch("postToolUse", {**generic, "tool_use_id": "two", "tool_output": "r"})
            _dispatch("postToolUse", {**generic, "tool_use_id": "one", "tool_output": "r"})

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["MCP: echo", "MCP: echo"]

    def test_partial_delivery_of_two_calls_keeps_both_spans(self, captured_spans, monkeypatch):
        """One call losing its dedicated pair and another losing its generic
        events must still produce a span each, even when they look identical.

        A single open generic invocation is not evidence that a dedicated
        result belongs to it — this is exactly the shape two half-delivered
        calls produce. Matching on the result as well as the call keeps them
        apart, and an unconsumed completion keeps its own span.
        """
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        generic = {**gen, "tool_name": "MCP:echo", "tool_input": {"text": "same"}}
        dedicated = {**gen, "tool_name": "echo", "tool_input": '{"text":"same"}'}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            # Call A: generic events only.
            _dispatch("preToolUse", {**generic, "tool_use_id": "A"})
            # Call B: dedicated events only, indistinguishable by call shape.
            _dispatch("beforeMCPExecution", dedicated)
            _dispatch("afterMCPExecution", {**dedicated, "result": "result-B"})
            _dispatch("postToolUse", {**generic, "tool_use_id": "A", "tool_output": "result-A"})

        spans = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for s in captured_spans]
        assert [span["name"] for span in spans] == ["MCP: echo", "Tool: MCP:echo"]
        assert _attrs(spans[0])["output.value"]["stringValue"] == "result-B"
        assert _attrs(spans[1])["output.value"]["stringValue"] == "result-A"

    def test_concurrent_dedicated_calls_keep_their_own_input_and_start(self, captured_spans, monkeypatch):
        """Overlapping dedicated calls must not swap arguments or start times.

        Pairing before/after by arrival order let each call adopt the other's
        record; the after-event claims its own by content instead.
        """
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        alpha = {**gen, "tool_name": "alpha", "tool_input": '{"call":"A"}'}
        beta = {**gen, "tool_name": "beta", "tool_input": '{"call":"B"}'}
        generic_alpha = {**gen, "tool_name": "MCP:alpha", "tool_input": {"call": "A"}, "tool_use_id": "A"}
        generic_beta = {**gen, "tool_name": "MCP:beta", "tool_input": {"call": "B"}, "tool_use_id": "B"}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", side_effect=itertools.count(1000, 10)):
            _dispatch("preToolUse", generic_alpha)
            _dispatch("preToolUse", generic_beta)
            _dispatch("beforeMCPExecution", alpha)
            _dispatch("beforeMCPExecution", beta)
            _dispatch("afterMCPExecution", {**alpha, "result": "result-A"})
            _dispatch("afterMCPExecution", {**beta, "result": "result-B"})
            _dispatch("postToolUse", {**generic_alpha, "tool_output": "result-A"})
            _dispatch("postToolUse", {**generic_beta, "tool_output": "result-B"})

        spans = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0] for s in captured_spans]
        by_name = {span["name"]: span for span in spans}
        assert set(by_name) == {"MCP: alpha", "MCP: beta"}
        for name, call, result in (("MCP: alpha", "A", "result-A"), ("MCP: beta", "B", "result-B")):
            attrs = _attrs(by_name[name])
            assert attrs["input.value"]["stringValue"] == f'{{"call":"{call}"}}'
            assert attrs["output.value"]["stringValue"] == result
        # Start times follow their own call: alpha began first, beta second.
        assert int(by_name["MCP: alpha"]["startTimeUnixNano"]) < int(by_name["MCP: beta"]["startTimeUnixNano"])

    def test_sequential_identical_calls_each_correlate(self, captured_spans, monkeypatch):
        """Identical calls that do not overlap stay unambiguous one at a time.

        The first invocation is closed before the second opens, so each result
        correlates to exactly one invocation and neither generic span emits.
        """
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        generic = {**gen, "tool_name": "MCP:echo", "tool_input": {"text": "same"}}
        dedicated = {**gen, "tool_name": "echo", "tool_input": '{"text":"same"}'}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            for invocation in ("one", "two"):
                _dispatch("preToolUse", {**generic, "tool_use_id": invocation})
                _dispatch("beforeMCPExecution", dedicated)
                _dispatch("afterMCPExecution", {**dedicated, "result": "r"})
                _dispatch("postToolUse", {**generic, "tool_use_id": invocation, "tool_output": "r"})

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["MCP: echo", "MCP: echo"]

    def test_mcp_correlation_state_never_stores_raw_content(self, captured_spans, monkeypatch):
        """Correlation bookkeeping persists digests only, never ids or text."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        gen = {"conversation_id": "c1", "generation_id": "g1"}
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch("preToolUse", {**gen, "tool_name": "MCP:echo", "tool_input": {"a": 1}, "tool_use_id": "raw-id"})
            _dispatch("beforeMCPExecution", {**gen, "tool_name": "echo", "tool_input": '{"secret":"s3cret-arg"}'})
            _dispatch(
                "afterMCPExecution",
                {**gen, "tool_name": "echo", "tool_input": '{"secret":"s3cret-arg"}', "result": "s3cret-result"},
            )

        files = [path for path in adapter.STATE_DIR.rglob("*") if path.is_file()]
        assert "raw-id" not in "\n".join(path.read_text(errors="replace") + str(path) for path in files)

        # Unlike the tool state file, whose captured arguments the privacy
        # switches govern, the correlation record holds neither the arguments
        # nor the result — only digests of them.
        records = [path for path in files if path.name.startswith("dedicateddone_")]
        assert records, "expected the digest-only dedicated-report record to be written"
        for path in records:
            content = path.read_text() + str(path)
            assert "s3cret-arg" not in content
            assert "s3cret-result" not in content

    def test_post_tool_use_emits_for_unknown_tool_name(self, captured_spans, monkeypatch):
        """postToolUse emits a span for tools not in the dedup set."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
        ):
            _dispatch(
                "postToolUse",
                {
                    "conversation_id": "c1",
                    "generation_id": "g1",
                    "toolName": "glob",
                    "toolInput": '{"pattern": "*.py"}',
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert span["name"] == "Tool: glob"
        assert attrs["tool.name"]["stringValue"] == "glob"
        assert attrs["input.value"]["stringValue"] == '{"pattern": "*.py"}'


# ---------------------------------------------------------------------------
# Deferred-state privacy boundary tests
# ---------------------------------------------------------------------------


class TestDeferredStatePrivacy:

    def test_disabled_prompt_logging_never_persists_raw_prompt(self, monkeypatch, _patch_cursor_state):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")

        _dispatch(
            "beforeSubmitPrompt",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "prompt": "PROMPT_DISK_SECRET",
            },
        )

        state_text = "\n".join(path.read_text() for path in _patch_cursor_state.rglob("*") if path.is_file())
        assert "PROMPT_DISK_SECRET" not in state_text
        assert "<redacted" in state_text

    def test_marker_shaped_raw_prompt_is_not_trusted_as_already_redacted(self, monkeypatch, _patch_cursor_state):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        raw_prompt = "<redacted (123 chars)>"

        _dispatch(
            "beforeSubmitPrompt",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "prompt": raw_prompt,
            },
        )

        state_text = "\n".join(path.read_text() for path in _patch_cursor_state.rglob("*") if path.is_file())
        assert raw_prompt not in state_text

    def test_creation_redaction_is_irreversible_if_terminal_policy_is_later_enabled(self, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "true")
        _dispatch(
            "beforeSubmitPrompt",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "prompt": "IRREVERSIBLE_PROMPT_SECRET",
            },
        )

        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        _dispatch(
            "afterAgentResponse",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "text": "allowed output",
            },
        )
        _dispatch("stop", {"conversation_id": "privacy-conv", "generation_id": "privacy-gen"})

        llm_payload = next(
            payload
            for payload in captured_spans
            if payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Agent Response"
        )
        assert "IRREVERSIBLE_PROMPT_SECRET" not in json.dumps(llm_payload)

    def test_disabled_tool_details_never_persists_raw_shell_command(self, monkeypatch, _patch_cursor_state):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")

        _dispatch(
            "beforeShellExecution",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "command": "printf SHELL_DISK_SECRET",
            },
        )

        state_text = "\n".join(path.read_text() for path in _patch_cursor_state.rglob("*") if path.is_file())
        assert "SHELL_DISK_SECRET" not in state_text
        assert "<redacted" in state_text

    def test_shell_creation_redaction_is_irreversible_when_after_event_repeats_command(
        self, captured_spans, monkeypatch
    ):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        command = "printf IRREVERSIBLE_SHELL_SECRET"
        _dispatch(
            "beforeShellExecution",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "command": command,
            },
        )

        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        after_payload = {
            "conversation_id": "privacy-conv",
            "generation_id": "privacy-gen",
            "command": command,
            "output": "SHELL_OUTPUT_SECRET",
            "exit_code": "0",
        }
        _dispatch("afterShellExecution", after_payload)
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        _dispatch("afterShellExecution", after_payload)

        shell_payloads = [
            payload
            for payload in captured_spans
            if payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Shell"
        ]
        assert len(shell_payloads) == 2
        for shell_payload in shell_payloads:
            shell_span = shell_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            attrs = {item["key"]: item["value"]["stringValue"] for item in shell_span["attributes"]}
            assert attrs["input.value"].startswith("<redacted")
            assert attrs["output.value"].startswith("<redacted")
            assert "IRREVERSIBLE_SHELL_SECRET" not in json.dumps(shell_payload)
            assert "SHELL_OUTPUT_SECRET" not in json.dumps(shell_payload)

    def test_delayed_shell_duplicate_after_stop_is_ignored_without_zombie_state(
        self, captured_spans, monkeypatch, _patch_cursor_state
    ):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        common = {"conversation_id": "privacy-conv", "generation_id": "privacy-gen"}
        command = "printf POST_CLEANUP_COMMAND_SECRET"
        after_payload = {
            **common,
            "command": command,
            "output": "POST_CLEANUP_OUTPUT_SECRET",
            "exit_code": "0",
        }

        _dispatch("beforeShellExecution", {**common, "command": command})
        _dispatch("afterShellExecution", after_payload)
        _dispatch("stop", common)
        shell_count = sum(
            payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Shell" for payload in captured_spans
        )

        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        _dispatch("beforeShellExecution", {**common, "command": command})
        _dispatch("afterShellExecution", after_payload)

        assert (
            sum(
                payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Shell"
                for payload in captured_spans
            )
            == shell_count
        )
        assert not list(_patch_cursor_state.glob("*privacy-gen*"))
        state_bytes = b"".join(path.read_bytes() for path in _patch_cursor_state.rglob("*") if path.is_file())
        assert b"POST_CLEANUP_COMMAND_SECRET" not in state_bytes
        assert b"POST_CLEANUP_OUTPUT_SECRET" not in state_bytes

    def test_stop_serializes_with_delayed_duplicate(self, captured_spans, monkeypatch, _patch_cursor_state):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "false")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        common = {"conversation_id": "race-conv", "generation_id": "race-gen"}
        after_payload = {
            **common,
            "command": "RACE_COMMAND_SECRET",
            "output": "RACE_OUTPUT_SECRET",
            "exit_code": "0",
        }
        _dispatch("beforeShellExecution", after_payload)
        _dispatch("afterShellExecution", after_payload)

        entered_mark = threading.Event()
        release_mark = threading.Event()
        duplicate_started = threading.Event()
        original_claim = getattr(sys.modules["tracing.cursor.hooks.handlers"], "generation_claim_terminal_event")

        def slow_claim(gen_id, event):
            entered_mark.set()
            assert release_mark.wait(timeout=5)
            return original_claim(gen_id, event)

        monkeypatch.setattr("tracing.cursor.hooks.handlers.generation_claim_terminal_event", slow_claim)
        monkeypatch.setenv("ARIZE_LOG_TOOL_DETAILS", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")

        stop_thread = threading.Thread(target=lambda: _dispatch("stop", common))

        def deliver_duplicate():
            duplicate_started.set()
            _dispatch("afterShellExecution", after_payload)

        duplicate_thread = threading.Thread(target=deliver_duplicate)
        stop_thread.start()
        assert entered_mark.wait(timeout=5)
        duplicate_thread.start()
        assert duplicate_started.wait(timeout=5)
        release_mark.set()
        stop_thread.join(timeout=5)
        duplicate_thread.join(timeout=5)

        assert not stop_thread.is_alive()
        assert not duplicate_thread.is_alive()
        shell_payloads = [
            payload
            for payload in captured_spans
            if payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Shell"
        ]
        assert len(shell_payloads) == 1
        assert not list(_patch_cursor_state.glob("*race-gen*"))
        state_bytes = b"".join(path.read_bytes() for path in _patch_cursor_state.rglob("*") if path.is_file())
        assert b"RACE_COMMAND_SECRET" not in state_bytes
        assert b"RACE_OUTPUT_SECRET" not in state_bytes

    def test_subagent_creation_redaction_is_irreversible_when_stop_repeats_task(self, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "false")
        task = "SUBAGENT_IRREVERSIBLE_SECRET"
        common = {
            "conversation_id": "privacy-conv",
            "generation_id": "privacy-gen",
            "subagent_type": "explore",
            "task": task,
        }
        _dispatch("subagentStart", common)

        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        stop_payload = {**common, "status": "completed", "summary": "SUBAGENT_SUMMARY_SECRET"}
        _dispatch("subagentStop", stop_payload)
        monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "true")
        _dispatch("subagentStop", stop_payload)

        subagent_payloads = [
            payload
            for payload in captured_spans
            if payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Subagent: explore"
        ]
        assert len(subagent_payloads) == 2
        for subagent_payload in subagent_payloads:
            subagent_span = subagent_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            attrs = {item["key"]: item["value"]["stringValue"] for item in subagent_span["attributes"]}
            assert attrs["input.value"].startswith("<redacted")
            assert attrs["output.value"].startswith("<redacted")
            assert task not in json.dumps(subagent_payload)
            assert "SUBAGENT_SUMMARY_SECRET" not in json.dumps(subagent_payload)

    def test_generic_tool_duplicate_post_is_suppressed_without_leaking(self, captured_spans, monkeypatch):
        """Duplicate delivery of one invocation's completion emits no second
        span, so a privacy policy loosened in between cannot expose content
        that was redacted when the invocation was created."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        tool_input = {"q": "GENERIC_DUPLICATE_SECRET"}
        common = {
            "conversation_id": "privacy-conv",
            "generation_id": "privacy-gen",
            "tool_use_id": "tool-privacy-1",
            "tool_name": "browser",
            "tool_input": tool_input,
        }
        _dispatch("preToolUse", common)

        post_payload = {**common, "tool_output": "GENERIC_OUTPUT_SECRET"}
        _dispatch("postToolUse", post_payload)
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        _dispatch("postToolUse", post_payload)

        tool_payloads = [
            payload
            for payload in captured_spans
            if payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Tool: browser"
        ]
        assert len(tool_payloads) == 1
        tool_span = tool_payloads[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {item["key"]: item["value"]["stringValue"] for item in tool_span["attributes"]}
        assert attrs["input.value"].startswith("<redacted")
        assert attrs["output.value"].startswith("<redacted")
        assert "GENERIC_DUPLICATE_SECRET" not in json.dumps(captured_spans)
        assert "GENERIC_OUTPUT_SECRET" not in json.dumps(captured_spans)

    def test_model_output_redaction_survives_duplicate_response(self, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "false")
        common = {"conversation_id": "privacy-conv", "generation_id": "privacy-gen"}
        _dispatch("beforeSubmitPrompt", {**common, "prompt": "allowed prompt"})
        response_payload = {**common, "text": "MODEL_DUPLICATE_SECRET", "model": "cursor-model"}
        _dispatch("afterAgentResponse", response_payload)

        monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "true")
        _dispatch("afterAgentResponse", response_payload)
        _dispatch("stop", common)

        llm_payloads = [
            payload
            for payload in captured_spans
            if payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Agent Response"
        ]
        assert len(llm_payloads) == 2
        for llm_payload in llm_payloads:
            llm_span = llm_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            attrs = {item["key"]: item["value"]["stringValue"] for item in llm_span["attributes"]}
            assert attrs["output.value"].startswith("<redacted")
            assert "MODEL_DUPLICATE_SECRET" not in json.dumps(llm_payload)

    def test_stop_reapplies_current_prompt_and_model_output_policy(self, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "true")
        monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "true")

        _dispatch(
            "beforeSubmitPrompt",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "prompt": "PROMPT_TERMINAL_SECRET",
            },
        )
        _dispatch(
            "afterAgentResponse",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "text": "MODEL_TERMINAL_SECRET",
            },
        )

        monkeypatch.setenv("ARIZE_LOG_PROMPTS", "false")
        monkeypatch.setenv("ARIZE_LOG_MODEL_OUTPUTS", "false")
        _dispatch(
            "stop",
            {"conversation_id": "privacy-conv", "generation_id": "privacy-gen"},
        )

        llm_payload = next(
            payload
            for payload in captured_spans
            if payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "Agent Response"
        )
        llm_span = llm_payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {item["key"]: item["value"]["stringValue"] for item in llm_span["attributes"]}
        assert attrs["input.value"].startswith("<redacted")
        assert attrs["output.value"].startswith("<redacted")
        assert "PROMPT_TERMINAL_SECRET" not in json.dumps(llm_payload)
        assert "MODEL_TERMINAL_SECRET" not in json.dumps(llm_payload)

    def test_post_tool_use_reapplies_current_tool_content_policy(self, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        _dispatch(
            "preToolUse",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "tool_use_id": "tool-1",
                "tool_name": "custom_search",
                "tool_input": {"query": "TOOL_TERMINAL_SECRET"},
            },
        )

        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        _dispatch(
            "postToolUse",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "tool_use_id": "tool-1",
                "tool_name": "custom_search",
                "tool_output": "TOOL_OUTPUT_TERMINAL_SECRET",
            },
        )

        payload = captured_spans[-1]
        span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {item["key"]: item["value"]["stringValue"] for item in span["attributes"]}
        assert attrs["input.value"].startswith("<redacted")
        assert attrs["output.value"].startswith("<redacted")
        assert "TOOL_TERMINAL_SECRET" not in json.dumps(payload)
        assert "TOOL_OUTPUT_TERMINAL_SECRET" not in json.dumps(payload)

    def test_after_mcp_reapplies_current_tool_content_policy(self, captured_spans, monkeypatch):
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        _dispatch(
            "beforeMCPExecution",
            {
                "conversation_id": "privacy-conv",
                "generation_id": "privacy-gen",
                "tool_name": "search",
                "tool_input": "MCP_TERMINAL_SECRET",
            },
        )

        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "false")
        after_payload = {
            "conversation_id": "privacy-conv",
            "generation_id": "privacy-gen",
            "tool_name": "search",
            "result": "MCP_OUTPUT_TERMINAL_SECRET",
        }
        _dispatch("afterMCPExecution", after_payload)
        monkeypatch.setenv("ARIZE_LOG_TOOL_CONTENT", "true")
        _dispatch("afterMCPExecution", after_payload)

        payloads = [
            payload
            for payload in captured_spans
            if payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "MCP: search"
        ]
        assert len(payloads) == 2
        for payload in payloads:
            span = payload["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            attrs = {item["key"]: item["value"]["stringValue"] for item in span["attributes"]}
            assert attrs["input.value"].startswith("<redacted")
            assert attrs["output.value"].startswith("<redacted")
            assert "MCP_TERMINAL_SECRET" not in json.dumps(payload)
            assert "MCP_OUTPUT_TERMINAL_SECRET" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# _handle_stop token count tests
# ---------------------------------------------------------------------------


class TestHandleStopTokenCounts:

    def test_stop_captures_token_counts(self, captured_spans, monkeypatch):
        """Stop payload with token fields produces llm.token_count.* attributes."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="root1"),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "status": "completed",
                    "input_tokens": 85919,
                    "output_tokens": 1523,
                    "cache_read_tokens": 68000,
                    "cache_write_tokens": 0,
                    "model": "claude-sonnet-4.5",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        # OpenInference: prompt is the total (input 85919 + cache_read 68000 +
        # cache_write 0 = 153919); cache split is reported via prompt_details.*.
        assert attrs["llm.token_count.prompt"]["intValue"] == 153919
        assert attrs["llm.token_count.completion"]["intValue"] == 1523
        assert attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 68000
        assert attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 0
        assert attrs["llm.token_count.total"]["intValue"] == 155442
        assert attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4.5"

    def test_stop_omits_token_attrs_when_payload_has_none(self, captured_spans, monkeypatch):
        """Stop payload without token fields produces no llm.token_count.* attributes."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "status": "completed",
                },
            )

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "llm.token_count.prompt" not in attr_keys
        assert "llm.token_count.completion" not in attr_keys
        assert "llm.token_count.prompt_details.cache_read" not in attr_keys
        assert "llm.token_count.prompt_details.cache_write" not in attr_keys
        assert "llm.token_count.total" not in attr_keys
        assert "llm.model_name" not in attr_keys

    def test_stop_token_count_handles_string_and_dash_values(self, captured_spans, monkeypatch):
        """String token values are coerced; '--' and None are omitted."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "input_tokens": "100",
                    "output_tokens": "--",
                    "cache_read_tokens": None,
                },
            )

        attrs = {
            a["key"]: a["value"]
            for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]
        }
        assert attrs["llm.token_count.prompt"]["intValue"] == 100
        assert "llm.token_count.completion" not in attrs
        assert "llm.token_count.prompt_details.cache_read" not in attrs
        assert "llm.token_count.total" not in attrs


# ---------------------------------------------------------------------------
# _handle_session_end tests
# ---------------------------------------------------------------------------


class TestHandleSessionEnd:

    def test_session_end_emits_chain_span_with_duration_and_status(self, captured_spans, monkeypatch):
        """sessionEnd produces a CHAIN span with duration, status, and reason."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="root-se"),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "sessionEnd",
                {
                    "conversation_id": "conv-end",
                    "generation_id": "gen-end",
                    "duration_ms": 7447445,
                    "final_status": "completed",
                    "reason": "window_close",
                },
            )

        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert span["name"] == "Session End"
        assert attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        assert attrs["cursor.session.duration_ms"]["intValue"] == 7447445
        assert attrs["cursor.session.final_status"]["stringValue"] == "completed"
        assert attrs["cursor.session.reason"]["stringValue"] == "window_close"
        assert attrs["session.id"]["stringValue"] == "conv-end"
        assert span["parentSpanId"] == "root-se"

    def test_session_end_cleans_up_generation(self, captured_spans, monkeypatch):
        """sessionEnd calls state_cleanup_generation with the gen_id."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation") as cleanup,
        ):
            _dispatch(
                "sessionEnd",
                {
                    "conversation_id": "conv-end",
                    "generation_id": "g-123",
                },
            )

        cleanup.assert_called_once_with("g-123")

    def test_session_end_handles_empty_payload(self, captured_spans, monkeypatch):
        """sessionEnd with only conversation_id emits a span without optional attrs."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "sessionEnd",
                {
                    "conversation_id": "conv-end",
                },
            )

        assert len(captured_spans) == 1
        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "session.id" in attr_keys
        assert "cursor.conversation.id" in attr_keys
        assert "cursor.session.duration_ms" not in attr_keys
        assert "cursor.session.final_status" not in attr_keys
        assert "cursor.session.reason" not in attr_keys
        assert "llm.token_count.prompt" not in attr_keys


# ---------------------------------------------------------------------------
# cursor.conversation.id attribute tests
# ---------------------------------------------------------------------------


class TestConversationIdAttribute:

    # Minimal payloads per event that produce at least one span
    _EVENT_PAYLOADS = {
        "beforeSubmitPrompt": {
            "hook_event_name": "beforeSubmitPrompt",
            "conversation_id": "conv-abc",
            # Missing generation exercises the safe immediate fallback.
            "prompt": "test",
        },
        # afterAgentResponse is excluded: under the deferred-LLM design it no longer
        # emits a span on its own — the Agent Response LLM span is flushed at stop.
        # cursor.conversation.id on that deferred span is covered by
        # TestDeferredLlmSpan below.
        "afterAgentThought": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-aat",
            "thought": "thinking",
        },
        "afterShellExecution": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-ase",
            "command": "ls",
            "output": "ok",
        },
        "afterMCPExecution": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-ame",
            "tool_name": "my_tool",
            "result": "ok",
        },
        "beforeReadFile": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-brf",
            "file_path": "/tmp/a.py",
        },
        "afterFileEdit": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-afe",
            "file_path": "/tmp/a.py",
        },
        "beforeTabFileRead": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-btfr",
            "file_path": "/tmp/a.py",
        },
        "afterTabFileEdit": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-atfe",
            "file_path": "/tmp/a.py",
        },
        "stop": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-stop",
            "status": "completed",
        },
        "sessionStart": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-ss",
            "cwd": "/tmp",
        },
        "sessionEnd": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-se",
        },
        "postToolUse": {
            "conversation_id": "conv-abc",
            "generation_id": "gen-ptu",
            "toolName": "glob",
            "toolInput": "*.py",
        },
    }

    # Events that push state but don't emit a span
    _NO_SPAN_EVENTS = {"beforeShellExecution", "beforeMCPExecution"}

    @pytest.mark.parametrize(
        "event",
        [e for e in _EVENT_PAYLOADS],
    )
    def test_conversation_id_attribute_on_every_handler(self, captured_spans, monkeypatch, event):
        """Every span-producing handler includes cursor.conversation.id."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_save"),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
            mock.patch("tracing.cursor.hooks.handlers.state_pop", return_value=None),
        ):
            _dispatch(event, self._EVENT_PAYLOADS[event])

        assert len(captured_spans) >= 1, f"No spans emitted for {event}"
        for sent in captured_spans:
            span = sent["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
            attrs = {a["key"]: a["value"] for a in span["attributes"]}
            assert (
                attrs.get("cursor.conversation.id", {}).get("stringValue") == "conv-abc"
            ), f"cursor.conversation.id missing or wrong on {event} span {span['name']}"

    def test_conversation_id_attribute_omitted_when_missing(self, captured_spans, monkeypatch):
        """When conversation_id is empty, cursor.conversation.id is not set."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {
                    "generation_id": "gen-1",
                    "status": "completed",
                },
            )

        attr_keys = {a["key"] for a in captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["attributes"]}
        assert "cursor.conversation.id" not in attr_keys


# ---------------------------------------------------------------------------
# IDE-safety regression test
# ---------------------------------------------------------------------------


class TestIdeSafety:

    def test_ide_payload_with_no_post_tool_use_unaffected(self, captured_spans, monkeypatch):
        """IDE dispatches before/afterShellExecution without postToolUse — exactly 1 span from after."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="root-ide"),
        ):
            _dispatch(
                "beforeShellExecution",
                {
                    "hook_event_name": "beforeShellExecution",
                    "conversation_id": "conv-ide",
                    "generation_id": "gen-ide",
                    "command": "echo hello",
                    "cwd": "/tmp",
                },
            )

        # beforeShellExecution emits no span
        assert len(captured_spans) == 0

        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value="root-ide"),
        ):
            _dispatch(
                "afterShellExecution",
                {
                    "hook_event_name": "afterShellExecution",
                    "conversation_id": "conv-ide",
                    "generation_id": "gen-ide",
                    "command": "echo hello",
                    "output": "hello",
                    "exit_code": "0",
                },
            )

        # afterShellExecution emits exactly one span
        assert len(captured_spans) == 1
        span = captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        assert span["name"] == "Shell"
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["openinference.span.kind"]["stringValue"] == "TOOL"
        assert attrs["tool.name"]["stringValue"] == "shell"


# ---------------------------------------------------------------------------
# Deferred LLM span tests (afterAgentResponse stashes; stop flushes)
# ---------------------------------------------------------------------------


def _spans_by_name(captured):
    """Flatten captured_spans into {name: [span_dict, ...]}."""
    out = {}
    for sent in captured:
        s = sent["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        out.setdefault(s["name"], []).append(s)
    return out


def _attrs(span):
    return {a["key"]: a["value"] for a in span["attributes"]}


class TestDeferredLlmSpan:
    """Per-turn token counts must land on Agent Response (LLM), not Agent Stop (CHAIN).

    The fix: afterAgentResponse stashes the LLM span; stop pops it, attaches the
    token counts from the stop payload, and sends the LLM span before Agent Stop.
    """

    def test_ide_happy_path_tokens_land_on_llm_span(self, captured_spans, monkeypatch):
        """IDE turn (beforeSubmit → after → stop with tokens): tokens on LLM span, none on Agent Stop."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "fix the bug",
                    "model_name": "claude-sonnet-4.5",
                },
            )

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=7000):
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "fixed",
                    "model_name": "claude-sonnet-4.5",
                },
            )

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000):
            _dispatch(
                "stop",
                {
                    "hook_event_name": "stop",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "status": "completed",
                    "input_tokens": 85919,
                    "output_tokens": 1523,
                    "cache_read_tokens": 68000,
                    "cache_write_tokens": 0,
                    "model": "claude-sonnet-4.5",
                },
            )

        spans = _spans_by_name(captured_spans)
        assert "User Prompt" in spans
        assert "Agent Response" in spans
        assert "Agent Stop" in spans

        llm_attrs = _attrs(spans["Agent Response"][0])
        assert llm_attrs["openinference.span.kind"]["stringValue"] == "LLM"
        # prompt = input 85919 + cache_read 68000 + cache_write 0 = 153919
        assert llm_attrs["llm.token_count.prompt"]["intValue"] == 153919
        assert llm_attrs["llm.token_count.completion"]["intValue"] == 1523
        assert llm_attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 68000
        assert llm_attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 0
        assert llm_attrs["llm.token_count.total"]["intValue"] == 155442
        assert llm_attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4.5"

        stop_attrs = _attrs(spans["Agent Stop"][0])
        assert stop_attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        for k in (
            "llm.token_count.prompt",
            "llm.token_count.completion",
            "llm.token_count.prompt_details.cache_read",
            "llm.token_count.prompt_details.cache_write",
            "llm.token_count.total",
        ):
            assert k not in stop_attrs, f"{k} should not be on Agent Stop CHAIN span"

    def test_stop_emits_llm_span_before_agent_stop(self, captured_spans, monkeypatch):
        """Order: Agent Response (LLM) is sent first, then Agent Stop (CHAIN)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "done",
                },
            )

        assert len(captured_spans) == 0

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=4000):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "input_tokens": 10,
                    "output_tokens": 5,
                },
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        # LLM must come before Agent Stop so strict OTLP backends see the parent first.
        assert names.index("Agent Response") < names.index("Agent Stop")

    def test_stop_fallback_keeps_tokens_on_chain_when_no_deferred_llm(self, captured_spans, monkeypatch):
        """No prior afterAgentResponse → CLI/sessionEnd-style behavior: Agent Stop carries tokens."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "input_tokens": 100,
                    "output_tokens": 50,
                    "cache_read_tokens": 25,
                    "cache_write_tokens": 5,
                    "model": "claude-sonnet-4.5",
                    "status": "completed",
                },
            )

        # Only the Agent Stop span is sent (no deferred LLM existed).
        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["Agent Stop"]
        stop_attrs = _attrs(captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert stop_attrs["openinference.span.kind"]["stringValue"] == "CHAIN"
        # prompt = input 100 + cache_read 25 + cache_write 5 = 130
        assert stop_attrs["llm.token_count.prompt"]["intValue"] == 130
        assert stop_attrs["llm.token_count.completion"]["intValue"] == 50
        assert stop_attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 25
        assert stop_attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 5
        assert stop_attrs["llm.token_count.total"]["intValue"] == 180
        assert stop_attrs["llm.model_name"]["stringValue"] == "claude-sonnet-4.5"

    def test_deferred_llm_dropped_when_stop_never_fires(self, captured_spans, monkeypatch):
        """beforeSubmit + afterAgentResponse without stop: deferred LLM span is never sent."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "p",
                },
            )
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "r",
                },
            )

        # afterAgentResponse sends the deferred root, but no Agent Response LLM yet.
        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert "Agent Response" not in names

    def test_stop_without_tokens_still_flushes_deferred_llm_without_token_attrs(self, captured_spans, monkeypatch):
        """If the stop payload has no tokens, the flushed LLM span has no llm.token_count.*."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "done",
                },
            )

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                },
            )

        spans = _spans_by_name(captured_spans)
        assert "Agent Response" in spans
        llm_attrs = _attrs(spans["Agent Response"][0])
        for k in (
            "llm.token_count.prompt",
            "llm.token_count.completion",
            "llm.token_count.prompt_details.cache_read",
            "llm.token_count.prompt_details.cache_write",
            "llm.token_count.total",
        ):
            assert k not in llm_attrs

    def test_zero_token_count_not_treated_as_absent(self, captured_spans, monkeypatch):
        """0 is a valid token count and must appear on the LLM span (no truthiness bugs)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "done",
                },
            )
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                },
            )

        spans = _spans_by_name(captured_spans)
        llm_attrs = _attrs(spans["Agent Response"][0])
        assert llm_attrs["llm.token_count.prompt"]["intValue"] == 0
        assert llm_attrs["llm.token_count.completion"]["intValue"] == 0
        assert llm_attrs["llm.token_count.prompt_details.cache_read"]["intValue"] == 0
        assert llm_attrs["llm.token_count.prompt_details.cache_write"]["intValue"] == 0
        assert llm_attrs["llm.token_count.total"]["intValue"] == 0

    def test_session_end_token_routing_unchanged(self, captured_spans, monkeypatch):
        """Without deferred LLM entries, tokens still attach to the Session End CHAIN span."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with (
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=9000),
            mock.patch("tracing.cursor.hooks.handlers.gen_root_span_get", return_value=""),
            mock.patch("tracing.cursor.hooks.handlers.state_cleanup_generation"),
        ):
            _dispatch(
                "sessionEnd",
                {
                    "conversation_id": "conv-end",
                    "generation_id": "gen-end",
                    "input_tokens": 200,
                    "output_tokens": 75,
                },
            )

        names = [s["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] for s in captured_spans]
        assert names == ["Session End"]
        attrs = _attrs(captured_spans[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0])
        assert attrs["llm.token_count.prompt"]["intValue"] == 200
        assert attrs["llm.token_count.completion"]["intValue"] == 75
        assert attrs["llm.token_count.total"]["intValue"] == 275

    def test_deferred_llm_uses_recorded_parent_and_start_time_at_stop(self, captured_spans, monkeypatch):
        """The flushed LLM span uses the parent and start_ms recorded at afterAgentResponse."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        # beforeSubmitPrompt records the root via gen_root_span_save (real disk).
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "beforeSubmitPrompt",
                {
                    "hook_event_name": "beforeSubmitPrompt",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "prompt": "p",
                },
            )

        # Capture the root span id that beforeSubmitPrompt persisted.
        from tracing.cursor.hooks.adapter import gen_root_span_get as real_get

        root_span_id = real_get("gen-1")
        assert root_span_id  # sanity

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2500):
            _dispatch(
                "afterAgentResponse",
                {
                    "hook_event_name": "afterAgentResponse",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "r",
                },
            )

        # stop runs at a much later timestamp — the LLM span must still use 2500.
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=99999):
            _dispatch(
                "stop",
                {
                    "hook_event_name": "stop",
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                },
            )

        spans = _spans_by_name(captured_spans)
        llm_span = spans["Agent Response"][0]
        assert llm_span["parentSpanId"] == root_span_id
        assert llm_span["startTimeUnixNano"] == "2500000000"
        assert llm_span["endTimeUnixNano"] == "2500000000"

    def test_multiple_deferred_llms_only_most_recent_gets_token_counts(self, captured_spans, monkeypatch):
        """Two afterAgentResponse events in one generation: each becomes an LLM span;
        tokens only attach to the most recent (last pushed = first popped)."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "first response",
                    "model_name": "claude-4",
                },
            )
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=2000):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "response": "second response",
                    "model_name": "claude-4",
                },
            )

        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=3000):
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-1",
                    "generation_id": "gen-1",
                    "input_tokens": 100,
                    "output_tokens": 20,
                },
            )

        spans = _spans_by_name(captured_spans)
        agent_response_spans = spans.get("Agent Response", [])
        assert len(agent_response_spans) == 2

        outputs = [_attrs(s).get("output.value", {}).get("stringValue") for s in agent_response_spans]
        # Identify which span carries tokens; that one must be the most recent
        # (second response). The other (first response) must have no token attrs.
        token_idx = next(i for i, s in enumerate(agent_response_spans) if "llm.token_count.prompt" in _attrs(s))
        no_token_idx = 1 - token_idx
        assert outputs[token_idx] == "second response"
        assert outputs[no_token_idx] == "first response"

        with_tokens = _attrs(agent_response_spans[token_idx])
        assert with_tokens["llm.token_count.prompt"]["intValue"] == 100
        assert with_tokens["llm.token_count.completion"]["intValue"] == 20
        assert with_tokens["llm.token_count.total"]["intValue"] == 120

        without = _attrs(agent_response_spans[no_token_idx])
        for k in (
            "llm.token_count.prompt",
            "llm.token_count.completion",
            "llm.token_count.total",
        ):
            assert k not in without

    def test_deferred_llm_carries_conversation_id_and_user_id(self, captured_spans, monkeypatch):
        """The flushed LLM span includes cursor.conversation.id and user.id when present."""
        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        # _resolve_user_id prefers env.user_id over payload user_email; clear env so
        # the test exercises the payload-only branch deterministically across machines.
        monkeypatch.setenv("ARIZE_USER_ID", "")
        with mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=1000):
            _dispatch(
                "afterAgentResponse",
                {
                    "conversation_id": "conv-abc",
                    "generation_id": "gen-1",
                    "response": "r",
                    "user_email": "alice@example.com",
                },
            )
            _dispatch(
                "stop",
                {
                    "conversation_id": "conv-abc",
                    "generation_id": "gen-1",
                    "user_email": "alice@example.com",
                },
            )

        spans = _spans_by_name(captured_spans)
        llm_attrs = _attrs(spans["Agent Response"][0])
        assert llm_attrs["session.id"]["stringValue"] == "conv-abc"
        assert llm_attrs["cursor.conversation.id"]["stringValue"] == "conv-abc"
        assert llm_attrs["user.id"]["stringValue"] == "alice@example.com"


# ---------------------------------------------------------------------------
# project.name injection
# ---------------------------------------------------------------------------


class TestProjectNameInjection:
    """project.name is injected onto every Cursor span, target-aware (issue #74)."""

    def _drive_and_capture(self, monkeypatch, event="beforeSubmitPrompt"):
        """Run a handler with the inner backend sender mocked so the send_span
        wrapper (which injects project.name) actually runs, and return the spans."""
        sent = []
        with (
            mock.patch(
                "tracing.cursor.hooks.handlers._send_span_to_backend",
                side_effect=lambda s: sent.append(s) or True,
            ),
            mock.patch("tracing.cursor.hooks.handlers.get_timestamp_ms", return_value=5000),
        ):
            _dispatch(
                event,
                {
                    "hook_event_name": event,
                    "conversation_id": "c1",
                    # Missing generation emits immediately without shared state.
                    "prompt": "hi",
                },
            )
        return sent

    def test_project_name_from_config_injected(self, monkeypatch):
        """config.json project_name lands on the span when no env override is set."""
        from core.common import env as core_env

        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        cfg = {"harnesses": {"cursor": {"project_name": "from-config", "target": "phoenix"}}}
        monkeypatch.setattr("core.config.load_config", lambda: cfg)
        core_env.invalidate_caches()

        sent = self._drive_and_capture(monkeypatch)

        assert len(sent) == 1
        span = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["project.name"]["stringValue"] == "from-config"

    def test_phoenix_project_env_injected(self, monkeypatch):
        """On the Phoenix backend, PHOENIX_PROJECT wins over config project_name."""
        from core.common import env as core_env

        monkeypatch.setenv("ARIZE_TRACE_ENABLED", "true")
        monkeypatch.delenv("ARIZE_PROJECT_NAME", raising=False)
        monkeypatch.setenv("PHOENIX_PROJECT", "from-phoenix-env")
        cfg = {"harnesses": {"cursor": {"project_name": "from-config", "target": "phoenix"}}}
        monkeypatch.setattr("core.config.load_config", lambda: cfg)
        core_env.invalidate_caches()

        sent = self._drive_and_capture(monkeypatch)

        span = sent[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
        attrs = {a["key"]: a["value"] for a in span["attributes"]}
        assert attrs["project.name"]["stringValue"] == "from-phoenix-env"
