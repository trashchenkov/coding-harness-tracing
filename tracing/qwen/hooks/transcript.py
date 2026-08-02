"""Parse Qwen Code transcript JSONL into harness-neutral typed events.

Qwen Code transcripts are a hybrid. The record envelope follows Claude Code —
``uuid`` / ``parentUuid`` / ``sessionId`` / ``type`` / ``timestamp`` / ``cwd`` /
``version`` — while the message body follows the Google GenAI convention that
Qwen Code inherited from Gemini CLI::

    message.parts = [
        {"text": "..."},
        {"functionCall":     {"id": ..., "name": ..., "args": {...}}},
        {"functionResponse": {"id": ..., "name": ..., "response": {"output": ...}}},
    ]

Claude Code instead uses typed content blocks (``tool_use`` / ``tool_result``),
so the body parsing here is written for Qwen rather than adapted.

Further Qwen specifics, verified against 0.21.3:

* the assistant role is ``model``, not ``assistant``;
* token usage lives in top-level ``usageMetadata`` with Gemini names
  (``promptTokenCount``, ``candidatesTokenCount``, ...);
* completed tool calls carry a top-level ``toolCallResult`` — Claude Code calls
  the same thing ``toolUseResult`` and shapes it differently;
* a subagent invocation is visible in the parent transcript as a tool result
  whose ``resultDisplay.type`` is ``task_execution``.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from core.event_model import (
    AgentEvent,
    BaseEvent,
    EventGraph,
    EventStatus,
    GraphDiagnostic,
    ModelCallEvent,
    ToolEvent,
    Usage,
)

# Record types we understand. Anything else (system/ui_telemetry,
# system/attribution_snapshot, ...) is skipped without complaint.
_ROLE_ASSISTANT = "model"
_TASK_EXECUTION = "task_execution"


def parse_qwen_transcript(
    transcript: Path,
    root_event: BaseEvent,
    *,
    start_line: int = 0,
) -> EventGraph:
    """Return a typed event graph for one main-agent or subagent transcript.

    ``start_line`` is a zero-based physical JSONL line offset. Unknown records
    and malformed lines are skipped with diagnostics rather than aborting the
    whole turn, matching the Claude Code parser's fail-soft contract.
    """
    graph = EventGraph([root_event])
    diagnostics: list[GraphDiagnostic] = []
    session_id = root_event.session_id
    turn_id = root_event.turn_id
    tools_by_call_id: dict[str, ToolEvent] = {}
    agent_id = root_event.agent_id if isinstance(root_event, AgentEvent) else None
    sequence = root_event.sequence + 1

    try:
        lines = transcript.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        graph.diagnostics = [
            GraphDiagnostic(
                code="transcript_read_error",
                message=str(exc),
                event_id=root_event.event_id,
            )
        ]
        return graph

    for line_index, raw_line in enumerate(lines):
        if line_index < max(0, start_line) or not raw_line.strip():
            continue
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError) as exc:
            diagnostics.append(
                GraphDiagnostic(
                    code="malformed_json",
                    message=f"line {line_index + 1}: {exc}",
                    event_id=root_event.event_id,
                    severity="warning",
                )
            )
            continue
        if not isinstance(entry, dict):
            continue

        record_type = entry.get("type")
        message = entry.get("message")
        parts = message.get("parts") if isinstance(message, dict) else None
        if not isinstance(parts, list):
            continue

        started_at = _timestamp_ms(entry.get("timestamp"))

        if record_type == "assistant":
            sequence = _absorb_assistant(
                entry,
                parts,
                graph=graph,
                tools_by_call_id=tools_by_call_id,
                agent_id=agent_id,
                sequence=sequence,
                started_at=started_at,
                parent_event_id=root_event.event_id,
                session_id=session_id,
                turn_id=turn_id,
            )
        elif record_type == "tool_result":
            _absorb_tool_result(entry, parts, tools_by_call_id, ended_at=started_at)

    graph.diagnostics = diagnostics + graph.validate()
    return graph


def _absorb_assistant(
    entry: dict[str, Any],
    parts: list[Any],
    *,
    graph: EventGraph,
    tools_by_call_id: dict[str, ToolEvent],
    agent_id: str | None,
    sequence: int,
    started_at: int | None,
    parent_event_id: str,
    session_id: str,
    turn_id: str,
) -> int:
    """Add one ModelCallEvent plus any tool calls it requested."""
    event_id = _string(entry.get("uuid"))
    if not event_id:
        return sequence

    model_event = ModelCallEvent(
        event_id=event_id,
        session_id=session_id,
        turn_id=turn_id,
        parent_event_id=parent_event_id,
        agent_id=agent_id,
        sequence=sequence,
        started_at_ms=started_at,
        ended_at_ms=started_at,
        model=_string(entry.get("model")),
        output=_parts_text(parts),
        usage=_usage(entry.get("usageMetadata")),
        status=EventStatus.COMPLETED,
    )
    graph.events.append(model_event)
    sequence += 1

    for part in parts:
        if not isinstance(part, dict):
            continue
        call = part.get("functionCall")
        if not isinstance(call, dict):
            continue
        call_id = _string(call.get("id"))
        if not call_id:
            continue
        tool_event = ToolEvent(
            event_id=f"{event_id}:{call_id}",
            session_id=session_id,
            turn_id=turn_id,
            parent_event_id=event_id,
            agent_id=agent_id,
            sequence=sequence,
            started_at_ms=started_at,
            ended_at_ms=None,
            tool_call_id=call_id,
            tool_name=_string(call.get("name")),
            input=call.get("args"),
            status=EventStatus.RUNNING,
        )
        graph.events.append(tool_event)
        tools_by_call_id[call_id] = tool_event
        sequence += 1

    return sequence


def _absorb_tool_result(
    entry: dict[str, Any],
    parts: list[Any],
    tools_by_call_id: dict[str, ToolEvent],
    *,
    ended_at: int | None,
) -> None:
    """Complete the ToolEvent that this result belongs to.

    The authoritative identifier is ``toolCallResult.callId``; the id inside
    ``functionResponse`` is used as a fallback for records that predate it.
    """
    result = entry.get("toolCallResult")
    result = result if isinstance(result, dict) else {}

    call_id = _string(result.get("callId"))
    if not call_id:
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("functionResponse"), dict):
                call_id = _string(part["functionResponse"].get("id"))
                if call_id:
                    break
    if not call_id:
        return

    tool_event = tools_by_call_id.get(call_id)
    if tool_event is None:
        return

    tool_event.ended_at_ms = ended_at
    tool_event.output = _response_output(parts)
    tool_event.status = EventStatus.COMPLETED

    # status is "success" on the happy path; error / errorType carry the failure.
    status = _string(result.get("status"))
    error = result.get("error")
    if error or (status and status != "success"):
        tool_event.status = EventStatus.FAILED
        tool_event.error = _string(error) or _string(result.get("errorType")) or status

    # A subagent invocation surfaces here rather than as a transcript flag:
    # Claude Code marks sidechain records inline, Qwen Code reports the child
    # through the tool result and hands the child transcript to SubagentStop.
    display = result.get("resultDisplay")
    if isinstance(display, dict) and display.get("type") == _TASK_EXECUTION:
        name = _string(display.get("subagentName"))
        if name:
            # The event model has no free-form metadata slot; source_id is the
            # documented place for a harness-side identifier.
            tool_event.source_id = name


# ---------------------------------------------------------------------------
# Part helpers — Google GenAI shapes
# ---------------------------------------------------------------------------


def _parts_text(parts: list[Any]) -> str:
    """Concatenate the plain-text parts of a message."""
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, dict):
            text = part.get("text")
            if isinstance(text, str) and text:
                chunks.append(text)
    return "\n".join(chunks)


def _response_output(parts: list[Any]) -> Any:
    """Extract the tool output carried by a functionResponse part."""
    for part in parts:
        if not isinstance(part, dict):
            continue
        response = part.get("functionResponse")
        if not isinstance(response, dict):
            continue
        payload = response.get("response")
        if isinstance(payload, dict) and "output" in payload:
            return payload["output"]
        return payload
    return None


def _usage(raw: Any) -> Usage:
    """Map Gemini-style usageMetadata onto the harness-neutral Usage record."""
    if not isinstance(raw, dict):
        return Usage()
    return Usage(
        input_tokens=_nonnegative_int(raw.get("promptTokenCount")),
        output_tokens=_nonnegative_int(raw.get("candidatesTokenCount")),
        cache_read_tokens=_nonnegative_int(raw.get("cachedContentTokenCount")),
        reported_total_tokens=_optional_nonnegative_int(raw.get("totalTokenCount")),
    )


# ---------------------------------------------------------------------------
# Scalar helpers
# ---------------------------------------------------------------------------


def _nonnegative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return number if number >= 0 else 0


def _optional_nonnegative_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number >= 0 else None


def _timestamp_ms(value: Any) -> int | None:
    """Parse an ISO-8601 timestamp into epoch milliseconds."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return int(parsed.timestamp() * 1000)


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""
