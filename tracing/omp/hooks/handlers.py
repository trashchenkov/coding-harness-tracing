#!/usr/bin/env python3
"""omp hook handlers: stateful event-forward tracing.

The TypeScript shim forwards each once-fired omp lifecycle event to this entry
point. This module keeps per-session trace state and emits Turn, LLM, and TOOL
spans without snapshot reconciliation or deduplication.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path
from typing import Any

from core.common import (
    FileLock,
    StateManager,
    build_span,
    debug_dump,
    env,
    error,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
    redact_content,
    send_span,
)
from tracing.omp.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    STATE_DIR,
    check_requirements,
    ensure_session_initialized,
    resolve_session,
    session_file_key,
)

MAX_CONTENT_CHARS = 65_536
MAX_METADATA_CHARS = 1_024
MAX_DURATION_MS = 86_400_000
MAX_SAFE_INTEGER = 2**53 - 1


def _bounded_text(value: Any, limit: int) -> str:
    """Return a bounded string with an explicit truncation marker."""
    text = value if isinstance(value, str) else ""
    if len(text) <= limit:
        return text
    marker = f"<truncated {len(text) - limit} chars>"
    return text[: max(0, limit - len(marker))] + marker


def _content_value(allowed: bool, value: Any) -> str:
    text = value if isinstance(value, str) else ""
    if not allowed:
        return redact_content(False, text)
    return _bounded_text(text, MAX_CONTENT_CHARS)


def _read_stdin() -> dict:
    """Read JSON from stdin. Return {} on empty or invalid input."""
    try:
        raw = sys.stdin.read()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError, ValueError):
        return {}


def _send_span_async(span_dict: dict) -> None:
    """Send a span without blocking the host process."""
    if os.environ.get("ARIZE_DISABLE_FORK", "").lower() == "true":
        send_span(span_dict)
        return
    if not hasattr(os, "fork"):
        send_span(span_dict)
        return

    try:
        pid = os.fork()
    except OSError:
        send_span(span_dict)
        return

    if pid > 0:
        try:
            os.waitpid(pid, 0)
        except OSError:
            # Best-effort reap: failure here should not interrupt the caller.
            pass
        return

    try:
        if os.fork() > 0:
            os._exit(0)
    except OSError:
        os._exit(0)

    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                # Best-effort stdio detachment in child process; ignore per-fd dup failures.
                pass
        os.close(devnull)
    except OSError as exc:
        # Best-effort stdio detachment in forked child; ignore failures to avoid
        # impacting host execution, but emit debug context for diagnostics.
        debug_dump("send_span", {"event": "omp_stdio_detach_failed", "error": str(exc)})

    try:
        send_span(span_dict)
    except Exception:
        # Best-effort tracing in detached child: never propagate failures.
        pass
    os._exit(0)


def _assistant_text(message: Any) -> str:
    """Concatenate text content items from an AssistantMessage."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    chunks = []
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text") or ""
            if text:
                chunks.append(str(text))
    return _bounded_text("".join(chunks), MAX_CONTENT_CHARS)


def _user_prompt(prompt_field: Any) -> str:
    """Return before_agent_start.prompt as-is when it is a string."""
    return _bounded_text(prompt_field, MAX_CONTENT_CHARS)


def _tool_calls(message: Any) -> dict:
    """Map ToolCall.id to its name and arguments."""
    calls: dict = {}
    if not isinstance(message, dict):
        return calls
    content = message.get("content")
    if not isinstance(content, list):
        return calls
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "toolCall":
            continue
        call_id = item.get("id")
        if not isinstance(call_id, str) or not call_id:
            continue
        arguments = item.get("arguments") or {}
        calls[call_id] = {
            "name": _bounded_text(item.get("name"), MAX_METADATA_CHARS),
            "arguments": arguments if isinstance(arguments, dict) else {},
        }
    return calls


def _text_of_content(content_list: Any) -> str:
    """Concatenate text content items from a ToolResultMessage content list."""
    if not isinstance(content_list, list):
        return ""
    chunks = []
    for item in content_list:
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text") or ""
            if text:
                chunks.append(str(text))
    return _bounded_text("".join(chunks), MAX_CONTENT_CHARS)


def _nonnegative_int(value: Any, maximum: int = MAX_SAFE_INTEGER) -> int | None:
    if type(value) is int and 0 <= value <= maximum:
        return value
    return None


def _turn_index_identity(value: Any) -> str:
    index = _nonnegative_int(value)
    if index is not None:
        return str(index)
    try:
        serialized = json.dumps(value, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        serialized = type(value).__name__
    digest = hashlib.sha256(serialized.encode("utf-8", errors="replace")).hexdigest()
    return f"invalid-{digest[:16]}"


def _tool_identity(turn_identity: str, call_id: Any, result_index: int) -> str:
    if isinstance(call_id, str) and call_id:
        digest = hashlib.sha256(call_id.encode("utf-8", errors="replace")).hexdigest()
        entity = f"call-{digest}"
    else:
        entity = f"result-{result_index}"
    return f"{turn_identity}:{entity}"


def _dispatch_lock_path(input_json: dict) -> Path:
    return STATE_DIR / f".dispatch_{session_file_key(input_json)}"


def _int_or_zero(value: Any) -> int:
    return _nonnegative_int(value) or 0


def _float_or_zero(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0.0
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else 0.0


def _timestamp_or_now(value: Any) -> int:
    timestamp = _nonnegative_int(value)
    return timestamp if timestamp and timestamp > 0 else get_timestamp_ms()


def _per_tool_attrs(tool_name: str, tool_input: Any) -> dict:
    """Return raw specialized tool attrs for known omp tools."""
    attrs: dict = {}
    if not isinstance(tool_input, dict):
        return attrs
    if tool_name == "bash":
        command = tool_input.get("command") or ""
        if command:
            attrs["tool.command"] = _bounded_text(str(command), MAX_CONTENT_CHARS)
    elif tool_name in ("read", "edit", "write"):
        file_path = tool_input.get("filePath") or tool_input.get("path") or ""
        if file_path:
            attrs["tool.file_path"] = _bounded_text(str(file_path), MAX_CONTENT_CHARS)
    elif tool_name in ("grep", "glob"):
        query = tool_input.get("pattern") or ""
        if query:
            attrs["tool.query"] = _bounded_text(str(query), MAX_CONTENT_CHARS)
    elif tool_name in ("webfetch", "web_fetch", "fetch"):
        url = tool_input.get("url") or ""
        if url:
            attrs["tool.url"] = _bounded_text(str(url), MAX_CONTENT_CHARS)
    return attrs


def _open_trace(state: StateManager, prompt: str) -> None:
    state.set("current_trace_id", generate_trace_id())
    state.set("current_trace_span_id", generate_span_id())
    state.set("current_trace_start_time", str(get_timestamp_ms()))
    state.set("current_trace_prompt", prompt)
    state.set("current_final_output", "")
    state.increment("trace_count")


def _clear_trace_state(state: StateManager) -> None:
    for key in (
        "current_trace_id",
        "current_trace_span_id",
        "current_trace_start_time",
        "current_trace_prompt",
        "current_final_output",
    ):
        state.delete(key)


def _emit_turn_root(state: StateManager, output_value: str) -> None:
    trace_id = state.get("current_trace_id")
    span_id = state.get("current_trace_span_id")
    if not trace_id or not span_id:
        return

    attrs: dict[str, Any] = {
        "session.id": _bounded_text(state.get("session_id"), MAX_METADATA_CHARS),
        "project.name": _bounded_text(state.get("project_name"), MAX_METADATA_CHARS),
        "openinference.span.kind": "CHAIN",
        "input.value": _content_value(env.log_prompts, state.get("current_trace_prompt") or ""),
        "output.value": _content_value(env.log_prompts, output_value),
    }
    user_id = _bounded_text(state.get("user_id"), MAX_METADATA_CHARS)
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Turn",
        "CHAIN",
        span_id,
        trace_id,
        "",
        state.get("current_trace_start_time") or str(get_timestamp_ms()),
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    _clear_trace_state(state)


def _close_pending_turn(state: StateManager, reason: str = "(closed by fail-safe)") -> None:
    """Emit the pending Turn root, preferring the latest assistant text."""
    if not state.get("current_trace_id") or not state.get("current_trace_span_id"):
        return
    final_output = state.get("current_final_output") or ""
    output_value = final_output if final_output else reason
    _emit_turn_root(state, output_value)


def _reserved_span_id(state: StateManager, key: str) -> str:
    """Return the state-reserved span ID for a replayable OMP entity."""
    existing = state.get(key)
    if existing:
        return existing
    span_id = generate_span_id()
    state.set(key, span_id)
    return span_id


def _emit_llm_span(state: StateManager, message: Any, turn_identity: str) -> str | None:
    if not isinstance(message, dict):
        return None
    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    if not trace_id or not parent_span_id:
        return None

    usage = message.get("usage") or {}
    if not isinstance(usage, dict):
        usage = {}
    cost_block = usage.get("cost") or {}
    if not isinstance(cost_block, dict):
        cost_block = {}

    input_tokens = _int_or_zero(usage.get("input"))
    output_tokens = _int_or_zero(usage.get("output"))
    cache_read = _int_or_zero(usage.get("cacheRead"))
    cache_write = _int_or_zero(usage.get("cacheWrite"))
    prompt_tokens = input_tokens + cache_read + cache_write
    reasoning_tokens = _int_or_zero(usage.get("reasoningTokens"))
    declared_total = _nonnegative_int(usage.get("totalTokens"))
    total_tokens = declared_total if declared_total is not None else prompt_tokens + output_tokens
    cost = _float_or_zero(cost_block.get("total"))

    end_ms = _timestamp_or_now(message.get("timestamp"))
    duration_ms = _nonnegative_int(message.get("duration"), MAX_DURATION_MS) or 0
    start_ms = end_ms - duration_ms if duration_ms else end_ms

    model = _bounded_text(message.get("model"), MAX_METADATA_CHARS)
    output_text = _assistant_text(message)
    attrs: dict[str, Any] = {
        "session.id": _bounded_text(state.get("session_id"), MAX_METADATA_CHARS),
        "project.name": _bounded_text(state.get("project_name"), MAX_METADATA_CHARS),
        "openinference.span.kind": "LLM",
        "llm.model_name": model,
        "llm.provider": _bounded_text(message.get("provider"), MAX_METADATA_CHARS),
        "llm.token_count.prompt": prompt_tokens,
        "llm.token_count.completion": output_tokens,
        "llm.token_count.total": total_tokens,
        "input.value": _content_value(env.log_prompts, state.get("current_trace_prompt") or ""),
        "output.value": _content_value(env.log_prompts, output_text),
        "tracing.turn_identity": turn_identity,
    }
    response_id = _bounded_text(message.get("responseId"), MAX_METADATA_CHARS)
    if response_id:
        attrs["llm.response_id"] = response_id
    if reasoning_tokens:
        attrs["llm.token_count.completion_details.reasoning"] = reasoning_tokens
    if cache_read:
        attrs["llm.token_count.prompt_details.cache_read"] = cache_read
    if cache_write:
        attrs["llm.token_count.prompt_details.cache_write"] = cache_write
    if cost:
        attrs["llm.cost"] = cost

    user_id = _bounded_text(state.get("user_id"), MAX_METADATA_CHARS)
    if user_id:
        attrs["user.id"] = user_id

    span_id = _reserved_span_id(state, f"span_llm_{turn_identity}")
    span = build_span(
        f"LLM: {model}" if model else "LLM",
        "LLM",
        span_id,
        trace_id,
        parent_span_id,
        start_ms,
        end_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    state.set("current_final_output", output_text)
    return span_id


def _emit_tool_span(
    state: StateManager,
    tool_result: dict,
    calls: dict,
    llm_span_id: str,
    tool_identity: str,
) -> None:
    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    if not trace_id or not parent_span_id:
        return

    raw_tool_name = tool_result.get("toolName")
    tool_name = _bounded_text(raw_tool_name, MAX_METADATA_CHARS) or "unknown"
    raw_call_id = tool_result.get("toolCallId")
    call_id = raw_call_id if isinstance(raw_call_id, str) else ""
    call = calls.get(call_id)
    arguments = call.get("arguments", {}) if isinstance(call, dict) else {}
    if not isinstance(arguments, dict):
        arguments = {}

    input_text = json.dumps(arguments)
    output_text = _text_of_content(tool_result.get("content"))
    attrs: dict[str, Any] = {
        "session.id": _bounded_text(state.get("session_id"), MAX_METADATA_CHARS),
        "project.name": _bounded_text(state.get("project_name"), MAX_METADATA_CHARS),
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "tool.call_id": _bounded_text(call_id, MAX_METADATA_CHARS),
        "tracing.parentage": "assistant_tool_call" if call is not None else "turn_fallback",
        "input.value": _content_value(env.log_tool_content, input_text),
        "output.value": _content_value(env.log_tool_content, output_text),
    }
    for key, value in _per_tool_attrs(tool_name, arguments).items():
        attrs[key] = _content_value(env.log_tool_details, value)

    user_id = _bounded_text(state.get("user_id"), MAX_METADATA_CHARS)
    if user_id:
        attrs["user.id"] = user_id

    is_error = bool(tool_result.get("isError"))
    status_message = _content_value(env.log_tool_content, output_text) if is_error else ""
    timestamp = _timestamp_or_now(tool_result.get("timestamp"))
    span = build_span(
        tool_name,
        "TOOL",
        _reserved_span_id(state, f"span_tool_{tool_identity}"),
        trace_id,
        llm_span_id if call is not None and llm_span_id else parent_span_id,
        timestamp,
        timestamp,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=2 if is_error else 1,
        status_message=status_message,
    )
    _send_span_async(span)
    state.increment("tool_count")


def _handle_before_agent_start(input_json: dict) -> None:
    debug_dump("omp_before_agent_start", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    if state.get("current_trace_id"):
        _close_pending_turn(state)
    _open_trace(state, _user_prompt(input_json.get("prompt")))


def _handle_turn_end(input_json: dict) -> None:
    debug_dump("omp_turn_end", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    if not state.get("current_trace_id"):
        _open_trace(state, "")

    trace_span_id = state.get("current_trace_span_id") or ""
    turn_index = _turn_index_identity(input_json.get("turnIndex"))
    turn_identity = f"{trace_span_id}:{turn_index}"
    emitted_key = f"emitted_turn_{turn_identity}"
    if state.get(emitted_key):
        return

    message = input_json.get("message")
    if not isinstance(message, dict):
        message = {}
    llm_span_id = _emit_llm_span(state, message, turn_identity) or ""

    calls = _tool_calls(message)
    tool_results = input_json.get("toolResults")
    if not isinstance(tool_results, list):
        tool_results = []
    for result_index, tool_result in enumerate(tool_results):
        if not isinstance(tool_result, dict):
            continue
        call_id = tool_result.get("toolCallId")
        tool_identity = _tool_identity(turn_identity, call_id, result_index)
        tool_emitted_key = f"emitted_tool_{tool_identity}"
        if state.get(tool_emitted_key):
            continue
        _emit_tool_span(state, tool_result, calls, llm_span_id, tool_identity)
        state.set(tool_emitted_key, "1")
    state.set(emitted_key, "1")


def _handle_agent_end(input_json: dict) -> None:
    debug_dump("omp_agent_end", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    if input_json.get("willContinue") is True:
        return
    if not state.get("current_trace_id") or not state.get("current_trace_span_id"):
        return

    # Prefer the final assistant text from agent_end's own authoritative payload.
    # Fall back to the state accumulator only when it carries no assistant text.
    final_output = ""
    messages = input_json.get("messages") or []
    if isinstance(messages, list):
        for message in reversed(messages):
            if isinstance(message, dict) and message.get("role") == "assistant":
                text = _assistant_text(message)
                if text:
                    final_output = text
                    break
    if not final_output:
        final_output = state.get("current_final_output") or ""

    _emit_turn_root(state, final_output)


def _handle_session_shutdown(input_json: dict) -> None:
    debug_dump("omp_session_shutdown", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    _close_pending_turn(state)


def main() -> None:
    """Hook entry point. Never raises."""
    try:
        if not check_requirements():
            return
        input_json = _read_stdin()
        if not input_json:
            return

        kind = input_json.get("type")
        session_id = _bounded_text(input_json.get("sessionId"), MAX_METADATA_CHARS)
        dispatch_lock = _dispatch_lock_path(input_json)
        try:
            with FileLock(dispatch_lock, timeout=5.0, break_on_timeout=False):
                if kind == "before_agent_start":
                    _handle_before_agent_start(input_json)
                elif kind == "turn_end":
                    _handle_turn_end(input_json)
                elif kind == "agent_end":
                    _handle_agent_end(input_json)
                elif kind == "session_shutdown":
                    _handle_session_shutdown(input_json)
                else:
                    log(f"omp: unknown type {kind!r}")
        except TimeoutError:
            error(f"omp: timed out serializing session {session_id!r}")
        except Exception as exc:
            error(f"omp {kind!r} failed: {exc!r}")
    except Exception as exc:
        error(f"omp main() crashed: {exc!r}")


if __name__ == "__main__":
    main()
