"""Qwen Code transcript parsing.

Fixtures mirror real 0.21.3 records: Claude-shaped envelope, Google GenAI
message bodies.
"""

import json

import pytest

from core.event_model import EventStatus, ModelCallEvent, ToolEvent, TurnEvent
from tracing.qwen.hooks.transcript import parse_qwen_transcript


def _root() -> TurnEvent:
    return TurnEvent(
        event_id="turn-1",
        session_id="sess-1",
        turn_id="trace-1",
        sequence=0,
        started_at_ms=1_000,
        ended_at_ms=None,
        status=EventStatus.RUNNING,
        input="do the thing",
    )


def _write(tmp_path, records):
    path = tmp_path / "chat.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return path


def _assistant(uuid, *, parts, usage=None, model="qwen3.5", ts="2026-08-02T17:55:30.757Z"):
    record = {
        "uuid": uuid,
        "parentUuid": None,
        "sessionId": "sess-1",
        "type": "assistant",
        "timestamp": ts,
        "cwd": "/tmp/project",
        "version": "0.21.3",
        "model": model,
        "message": {"role": "model", "parts": parts},
    }
    if usage is not None:
        record["usageMetadata"] = usage
    return record


def _tool_result(uuid, call_id, *, name, output, result=None, ts="2026-08-02T17:55:34.942Z"):
    return {
        "uuid": uuid,
        "sessionId": "sess-1",
        "type": "tool_result",
        "timestamp": ts,
        "message": {
            "role": "user",
            "parts": [{"functionResponse": {"id": call_id, "name": name, "response": {"output": output}}}],
        },
        "toolCallResult": result if result is not None else {"callId": call_id, "status": "success"},
    }


def test_assistant_text_becomes_a_model_call(tmp_path):
    path = _write(tmp_path, [_assistant("a1", parts=[{"text": "hello"}])])

    graph = parse_qwen_transcript(path, _root())

    calls = [e for e in graph.events if isinstance(e, ModelCallEvent)]
    assert len(calls) == 1
    assert calls[0].output == "hello"
    assert calls[0].model == "qwen3.5"
    assert calls[0].parent_event_id == "turn-1"


def test_gemini_usage_names_map_onto_the_shared_usage_record(tmp_path):
    """Qwen reports Gemini-named counters, not Claude's input/output tokens."""
    path = _write(
        tmp_path,
        [
            _assistant(
                "a1",
                parts=[{"text": "hi"}],
                usage={
                    "promptTokenCount": 100,
                    "candidatesTokenCount": 7,
                    "totalTokenCount": 107,
                    "cachedContentTokenCount": 20,
                },
            )
        ],
    )

    usage = [e for e in parse_qwen_transcript(path, _root()).events if isinstance(e, ModelCallEvent)][0].usage

    assert usage.input_tokens == 100
    assert usage.output_tokens == 7
    assert usage.cache_read_tokens == 20
    assert usage.total_tokens == 107


def test_function_call_and_response_pair_into_one_completed_tool(tmp_path):
    path = _write(
        tmp_path,
        [
            _assistant(
                "a1",
                parts=[{"functionCall": {"id": "call_1", "name": "run_shell_command", "args": {"command": "ls"}}}],
            ),
            _tool_result("t1", "call_1", name="run_shell_command", output="file.txt"),
        ],
    )

    tools = [e for e in parse_qwen_transcript(path, _root()).events if isinstance(e, ToolEvent)]

    assert len(tools) == 1
    tool = tools[0]
    assert tool.tool_name == "run_shell_command"
    assert tool.tool_call_id == "call_1"
    assert tool.input == {"command": "ls"}
    assert tool.output == "file.txt"
    assert tool.status is EventStatus.COMPLETED
    assert tool.parent_event_id == "a1"
    assert tool.ended_at_ms is not None


def test_tool_failure_is_read_from_tool_call_result(tmp_path):
    """Qwen carries failures in toolCallResult, where Claude uses toolUseResult."""
    path = _write(
        tmp_path,
        [
            _assistant("a1", parts=[{"functionCall": {"id": "call_1", "name": "read_file", "args": {}}}]),
            _tool_result(
                "t1",
                "call_1",
                name="read_file",
                output=None,
                result={
                    "callId": "call_1",
                    "status": "error",
                    "error": "ENOENT: no such file",
                    "errorType": "FILE_NOT_FOUND",
                },
            ),
        ],
    )

    tool = [e for e in parse_qwen_transcript(path, _root()).events if isinstance(e, ToolEvent)][0]

    assert tool.status is EventStatus.FAILED
    assert "ENOENT" in (tool.error or "")


def test_subagent_invocation_is_recovered_from_the_result_display(tmp_path):
    """A task_execution result names the subagent that ran."""
    path = _write(
        tmp_path,
        [
            _assistant("a1", parts=[{"functionCall": {"id": "call_9", "name": "agent", "args": {"prompt": "2+2"}}}]),
            _tool_result(
                "t1",
                "call_9",
                name="agent",
                output="4",
                result={
                    "callId": "call_9",
                    "status": "success",
                    "resultDisplay": {"type": "task_execution", "subagentName": "general-purpose"},
                },
            ),
        ],
    )

    tool = [e for e in parse_qwen_transcript(path, _root()).events if isinstance(e, ToolEvent)][0]

    assert tool.source_id == "general-purpose"


def test_system_records_are_skipped(tmp_path):
    """ui_telemetry and attribution_snapshot have no Claude Code equivalent."""
    path = _write(
        tmp_path,
        [
            {"uuid": "s1", "type": "system", "subtype": "ui_telemetry", "message": {"parts": []}},
            {"uuid": "s2", "type": "system", "subtype": "attribution_snapshot", "message": {"parts": []}},
            _assistant("a1", parts=[{"text": "only me"}]),
        ],
    )

    graph = parse_qwen_transcript(path, _root())

    assert len([e for e in graph.events if isinstance(e, ModelCallEvent)]) == 1


def test_start_line_skips_earlier_turns(tmp_path):
    path = _write(
        tmp_path,
        [
            _assistant("old", parts=[{"text": "previous turn"}]),
            _assistant("new", parts=[{"text": "this turn"}]),
        ],
    )

    calls = [e for e in parse_qwen_transcript(path, _root(), start_line=1).events if isinstance(e, ModelCallEvent)]

    assert [c.event_id for c in calls] == ["new"]


def test_malformed_lines_are_reported_not_fatal(tmp_path):
    path = tmp_path / "chat.jsonl"
    path.write_text("{not json\n" + json.dumps(_assistant("a1", parts=[{"text": "ok"}])) + "\n", encoding="utf-8")

    graph = parse_qwen_transcript(path, _root())

    assert len([e for e in graph.events if isinstance(e, ModelCallEvent)]) == 1
    assert any(d.code == "malformed_json" for d in graph.diagnostics)


def test_missing_transcript_yields_a_diagnostic(tmp_path):
    graph = parse_qwen_transcript(tmp_path / "absent.jsonl", _root())

    assert [d.code for d in graph.diagnostics] == ["transcript_read_error"]


@pytest.mark.parametrize("payload", ["", "   \n\n", "[]"])
def test_empty_or_non_record_transcripts_produce_only_the_root(tmp_path, payload):
    path = tmp_path / "chat.jsonl"
    path.write_text(payload, encoding="utf-8")

    graph = parse_qwen_transcript(path, _root())

    assert [e.event_id for e in graph.events] == ["turn-1"]
