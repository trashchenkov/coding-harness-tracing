"""Contract tests for the Claude JSONL -> typed event graph parser."""

import json
from pathlib import Path

from core.event_model import AgentEvent, EventStatus, ModelCallEvent, ToolEvent, TurnEvent
from tracing.claude_code.hooks.transcript import parse_claude_transcript


FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _turn(session_id: str = "session-main-1") -> TurnEvent:
    return TurnEvent(
        event_id="turn-1",
        session_id=session_id,
        turn_id="turn-1",
        sequence=0,
        started_at_ms=1_767_268_800_000,
        ended_at_ms=None,
        status=EventStatus.RUNNING,
        input="synthetic prompt",
    )


def _agent() -> AgentEvent:
    return AgentEvent(
        event_id="agent:agent-1",
        parent_event_id="turn-1",
        session_id="session-agent-1",
        turn_id="turn-1",
        sequence=1,
        started_at_ms=1_767_272_401_000,
        ended_at_ms=None,
        status=EventStatus.RUNNING,
        agent_id="agent-1",
        input="read hello.py",
    )


def _typed(events, event_type):
    return [event for event in events if isinstance(event, event_type)]


def test_main_fixture_restores_each_model_call_and_exact_tool_parent():
    graph = parse_claude_transcript(FIXTURE_DIR / "main_tool_cycle.jsonl", _turn())

    models = _typed(graph.events, ModelCallEvent)
    tools = _typed(graph.events, ToolEvent)

    assert [model.event_id for model in models] == [
        "assistant-main-1",
        "assistant-main-2",
        "assistant-main-3",
    ]
    assert [model.parent_event_id for model in models] == ["turn-1"] * 3
    assert [tool.tool_call_id for tool in tools] == ["tool-read-1", "tool-bash-1"]
    assert [tool.parent_event_id for tool in tools] == ["assistant-main-1", "assistant-main-2"]
    assert [tool.tool_name for tool in tools] == ["Read", "Bash"]
    assert tools[0].output == "def greeting():\n    return \"SYNTHETIC_TOOL_OK\""
    assert tools[1].output == "SYNTHETIC_TOOL_OK"
    assert all(tool.status is EventStatus.COMPLETED for tool in tools)
    assert graph.validate() == []


def test_model_usage_and_text_are_kept_per_call():
    graph = parse_claude_transcript(FIXTURE_DIR / "main_tool_cycle.jsonl", _turn())
    models = _typed(graph.events, ModelCallEvent)

    assert models[0].model == "qwen3-coder-next"
    assert models[0].usage.input_tokens == 100
    assert models[0].usage.output_tokens == 20
    assert models[0].usage.cache_read_tokens == 10
    assert models[0].usage.cache_write_tokens == 5
    assert models[0].usage.total_tokens == 120
    assert models[0].output == ""
    assert models[2].output == "SYNTHETIC_TOOL_OK"
    assert models[2].status is EventStatus.COMPLETED


def test_start_line_excludes_prior_model_and_tool_cycles():
    graph = parse_claude_transcript(FIXTURE_DIR / "main_tool_cycle.jsonl", _turn(), start_line=3)

    assert [event.event_id for event in _typed(graph.events, ModelCallEvent)] == [
        "assistant-main-2",
        "assistant-main-3",
    ]
    assert [event.tool_call_id for event in _typed(graph.events, ToolEvent)] == ["tool-bash-1"]


def test_subagent_transcript_uses_agent_as_root_and_keeps_agent_id():
    graph = parse_claude_transcript(FIXTURE_DIR / "subagent_agent.jsonl", _agent())

    models = _typed(graph.events, ModelCallEvent)
    tools = _typed(graph.events, ToolEvent)

    assert graph.events[0].event_id == "agent:agent-1"
    assert [model.parent_event_id for model in models] == ["agent:agent-1", "agent:agent-1"]
    assert all(model.agent_id == "agent-1" for model in models)
    assert tools[0].parent_event_id == "assistant-subagent-1"
    assert tools[0].agent_id == "agent-1"
    assert tools[0].output.startswith("def greeting")


def test_main_agent_tool_result_preserves_agent_correlation_metadata():
    root = _turn(session_id="session-agent-1")
    graph = parse_claude_transcript(FIXTURE_DIR / "subagent_main.jsonl", root)
    tool = _typed(graph.events, ToolEvent)[0]

    assert tool.tool_name == "Agent"
    assert tool.tool_call_id == "tool-agent-1"
    assert tool.input["subagent_type"] == "synthetic-explorer"
    assert tool.input["run_in_background"] is False
    assert tool.output["toolUseResult"]["agentId"] == "agent-1"
    assert tool.output["toolUseResult"]["agentType"] == "synthetic-explorer"


def test_malformed_unknown_and_partial_records_fail_soft(tmp_path: Path):
    transcript = tmp_path / "partial.jsonl"
    transcript.write_text(
        "not-json\n"
        + json.dumps({"type": "future-record", "payload": {"x": 1}})
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "timestamp": "not-a-time",
                "message": {
                    "role": "assistant",
                    "model": "m",
                    "content": "legacy text",
                    "usage": {"input_tokens": "bad", "output_tokens": 2},
                },
            }
        )
        + "\n"
    )

    graph = parse_claude_transcript(transcript, _turn())
    models = _typed(graph.events, ModelCallEvent)

    assert len(models) == 1
    assert models[0].output == "legacy text"
    assert models[0].usage.input_tokens == 0
    assert models[0].usage.output_tokens == 2
    assert any(diagnostic.code == "malformed_json" for diagnostic in graph.diagnostics)
    assert any(diagnostic.code == "missing_source_id" for diagnostic in graph.diagnostics)
    assert any(diagnostic.code == "invalid_timestamp" for diagnostic in graph.diagnostics)


def test_error_tool_result_marks_failed_and_preserves_error(tmp_path: Path):
    transcript = tmp_path / "failed.jsonl"
    rows = [
        {
            "type": "assistant",
            "uuid": "assistant-failed-1",
            "timestamp": "2026-01-01T12:00:01.000Z",
            "sessionId": "session-main-1",
            "message": {
                "role": "assistant",
                "model": "m",
                "content": [{"type": "tool_use", "id": "tool-failed-1", "name": "Bash", "input": {}}],
                "usage": {},
            },
        },
        {
            "type": "user",
            "uuid": "result-failed-1",
            "timestamp": "2026-01-01T12:00:02.000Z",
            "sessionId": "session-main-1",
            "message": {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-failed-1", "content": "command failed", "is_error": True}
                ],
            },
        },
    ]
    transcript.write_text("\n".join(json.dumps(row) for row in rows) + "\n")

    graph = parse_claude_transcript(transcript, _turn())
    tool = _typed(graph.events, ToolEvent)[0]

    assert tool.status is EventStatus.FAILED
    assert tool.error == "command failed"
    assert tool.output == "command failed"
