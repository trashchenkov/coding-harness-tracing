#!/usr/bin/env python3
"""Gemini hook handlers. One exported function per Gemini hook event.

Single-mode (CLI-only). Each entry point reads stdin JSON, runs the handler
in a try/except, and prints {} to stdout in finally.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys

from core.common import (
    FileLock,
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
from tracing.gemini.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    dispatch_lock_path,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_session,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _read_stdin() -> dict:
    """Read JSON from stdin. Returns {} on empty/invalid input."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _print_response() -> None:
    """Print {} to stdout. Same for all 8 events."""
    print(json.dumps({}))


def _get_robust(data: dict, *keys, default=None):
    """Try both snake_case and camelCase variants of keys."""
    for key in keys:
        if key in data:
            return data[key]
        if "_" in key:
            parts = key.split("_")
            camel = parts[0] + "".join(x.capitalize() for x in parts[1:])
            if camel in data:
                return data[camel]
    return default


def _iter_robust_values(data: dict, *keys):
    """Yield every present snake/camel variant without first-key masking."""
    seen = set()
    for key in keys:
        variants = [key]
        if "_" in key:
            parts = key.split("_")
            variants.append(parts[0] + "".join(x.capitalize() for x in parts[1:]))
        for variant in variants:
            if variant in data and variant not in seen:
                seen.add(variant)
                yield data[variant]


def _extract_text(obj) -> str:
    """Extract string content from various nested structures.

    Recognized keys (in priority order):
      candidates / parts / text / content — Gemini API response shape
      llmContent — Gemini CLI tool_response canonical content
      returnDisplay — Gemini CLI tool_response UI fallback
    """
    if obj is None:
        return ""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        return "\n".join(_extract_text(item) for item in obj)
    if isinstance(obj, dict):
        if "candidates" in obj:
            return _extract_text(obj["candidates"])
        if "parts" in obj:
            return _extract_text(obj["parts"])
        if "text" in obj:
            return _extract_text(obj["text"])
        if "content" in obj:
            return _extract_text(obj["content"])
        if "llmContent" in obj:
            return _extract_text(obj["llmContent"])
        if "returnDisplay" in obj:
            return _extract_text(obj["returnDisplay"])
    return str(obj)


def _extract_response_text(value) -> str:
    """Extract only recognized model-response content, ignoring metadata-only mappings."""
    if value is None or value == {} or value == []:
        return ""
    if isinstance(value, list):
        return "\n".join(filter(None, (_extract_response_text(item) for item in value)))
    if isinstance(value, dict):
        for key in ("content", "text", "parts", "candidates", "message"):
            if key in value:
                text = _extract_response_text(value[key])
                if text:
                    return text
        return ""
    return _extract_text(value)


def _first_response_text(input_json: dict, *keys: str) -> str:
    """Return the first alias containing recognized, non-empty response content."""
    for value in _iter_robust_values(input_json, *keys):
        text = _extract_response_text(value)
        if text:
            return text
    return ""


def _response_aliases(input_json: dict):
    """Yield mapping-valued model response aliases in contract priority order."""
    seen = set()
    for key in ("llm_response", "response", "model_response"):
        for value in _iter_robust_values(input_json, key):
            if isinstance(value, dict) and id(value) not in seen:
                seen.add(id(value))
                yield value


def _usage_objects(input_json: dict):
    """Yield all explicit usage mappings in response-alias priority order."""
    for source in (*_response_aliases(input_json), input_json):
        for usage in _iter_robust_values(source, "usage_metadata", "usage"):
            if isinstance(usage, dict):
                yield usage


def _usage_object(input_json: dict) -> dict | None:
    """Return the first explicit usage mapping, including an empty mapping."""
    return next(_usage_objects(input_json), None)


def _extract_token_updates(input_json: dict) -> tuple[int | None, int | None, int | None, int | None]:
    """Resolve each valid token counter independently across response aliases."""
    usages = list(_usage_objects(input_json))
    if not usages:
        return None, None, None, None

    def _valid(*names: str) -> int | None:
        for usage in usages:
            for value in _iter_robust_values(usage, *names):
                if type(value) is int and value >= 0:
                    return value
        return None

    return (
        _valid("prompt_token_count", "prompt_tokens"),
        _valid("candidates_token_count", "candidates_tokens", "output_tokens"),
        _valid("thoughts_token_count", "reasoning_tokens"),
        _valid("total_token_count", "total_tokens"),
    )


def _extract_tokens(input_json: dict) -> tuple[int, int, int]:
    """Extract prompt, completion (including reasoning), and authoritative total."""
    prompt, candidates, reasoning, total = _extract_token_updates(input_json)
    return prompt or 0, (candidates or 0) + (reasoning or 0), total or 0


def _has_usage(input_json: dict) -> bool:
    """Whether this event explicitly carries a usage object, including empty/zeros."""
    return _usage_object(input_json) is not None


def _is_model_final(input_json: dict) -> bool:
    """Detect well-typed terminal markers consistently across response aliases."""
    for source in (input_json, *_response_aliases(input_json)):
        if any(value is True for value in _iter_robust_values(source, "is_final", "final", "done")):
            return True
        if any(
            isinstance(value, str) and bool(value.strip()) for value in _iter_robust_values(source, "finish_reason")
        ):
            return True
        for candidates in _iter_robust_values(source, "candidates"):
            if not isinstance(candidates, list):
                continue
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                if any(
                    isinstance(value, str) and bool(value.strip())
                    for value in _iter_robust_values(candidate, "finish_reason")
                ):
                    return True
    return False


def _tool_pending_key(input_json: dict) -> tuple[str, bool]:
    """Return a stable pending-call key and whether Gemini supplied a call ID."""
    call_id = _get_robust(input_json, "tool_call_id")
    if isinstance(call_id, str) and call_id:
        return f"tool_{call_id}_start", True
    tool_name = _get_robust(input_json, "tool_name", default="unknown")
    tool_input = _get_robust(input_json, "tool_input", "tool_args", "args") or {}
    canonical = json.dumps([tool_name, tool_input], sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"tool_pending_{digest}", False


def _append_tool_start(state, key: str, start_time: str) -> None:
    """Append a start timestamp to a FIFO encoded in state."""
    raw = state.get(key)
    try:
        pending = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        pending = []
    if not isinstance(pending, list):
        pending = []
    pending.append(start_time)
    state.set(key, json.dumps(pending))


def _pop_tool_start(state, key: str) -> str | None:
    """Pop the oldest timestamp from a pending-call FIFO."""
    raw = state.get(key)
    try:
        pending = json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        pending = []
    if not isinstance(pending, list) or not pending:
        return None
    start_time = str(pending.pop(0))
    if pending:
        state.set(key, json.dumps(pending))
    else:
        state.delete(key)
    return start_time


# ---------------------------------------------------------------------------
# Internal handler implementations
# ---------------------------------------------------------------------------


_DEFERRED_SPANS: list[dict] | None = None


def _export_span_async(span_dict: dict) -> None:
    """Export a span, detaching the network send where fork is available.

    Gemini invokes hooks synchronously and waits for the subprocess to exit
    before resuming its own response stream. The slowest part of a hook is
    the OTLP POST in send_span (up to ~10s). Double-fork detaches a
    grandchild that's reparented to init/launchd; the parent returns
    immediately so the hook exits in milliseconds.

    Falls back to synchronous send when fork() is unavailable (Windows) or
    when ARIZE_DISABLE_FORK=true (used by tests so spans are visible to
    captured_spans fixtures in the parent process).
    """
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
        # Parent: reap the immediate child quickly (it forks-and-exits).
        try:
            os.waitpid(pid, 0)
        except OSError:
            pass
        return

    # First child: fork again and exit so the grandchild has no parent.
    try:
        if os.fork() > 0:
            os._exit(0)
    except OSError:
        os._exit(0)

    # Grandchild: redirect stdio so we can't pollute Gemini's pipes,
    # then perform the actual network send.
    try:
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):
            try:
                os.dup2(devnull, fd)
            except OSError:
                pass
        os.close(devnull)
    except OSError:
        pass
    try:
        send_span(span_dict)
    except Exception:
        pass
    os._exit(0)


def _send_span_async(span_dict: dict) -> None:
    """Queue spans during a serialized hook transaction; otherwise export now."""
    if _DEFERRED_SPANS is not None:
        _DEFERRED_SPANS.append(span_dict)
        return
    _export_span_async(span_dict)


# ---------------------------------------------------------------------------
# Model-call accumulation: Gemini fires AfterModel once per streaming chunk.
# We coalesce all chunks for a single BeforeModel into one LLM span so Arize
# shows one row per model call instead of N near-empty rows.
# ---------------------------------------------------------------------------


_MODEL_STATE_KEYS = ("start", "prompt", "response", "model", "p_tokens", "c_tokens", "r_tokens", "t_tokens")


def _clear_model_state(state, model_call_id: str) -> None:
    """Remove all per-call accumulator keys plus the current_model_call_id pointer."""
    for suffix in _MODEL_STATE_KEYS:
        state.delete(f"model_{model_call_id}_{suffix}")
    if state.get("current_model_call_id") == model_call_id:
        state.delete("current_model_call_id")


def _flush_pending_model_call(state) -> None:
    """Emit one LLM span for the accumulated model call, then clear its state.

    No-op if no model call is pending or the trace is no longer active.
    Called when a final-chunk arrives, when a new BeforeModel begins, when
    AfterAgent fires, and when a turn is force-closed by the fail-safe.
    """
    model_call_id = state.get("current_model_call_id")
    if not model_call_id:
        return

    trace_id = state.get("current_trace_id")
    if not trace_id:
        # No active turn; drop accumulators silently rather than emit a
        # dangling LLM span.
        _clear_model_state(state, model_call_id)
        return

    parent_span_id = state.get("current_trace_span_id") or ""
    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""

    start_time = state.get(f"model_{model_call_id}_start") or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())
    model_name = state.get(f"model_{model_call_id}_model") or ""
    prompt_str = state.get(f"model_{model_call_id}_prompt") or state.get("current_trace_prompt") or ""
    response_str = state.get(f"model_{model_call_id}_response") or ""
    if response_str:
        # Per-call state is deleted after emission, but a cancellation
        # fail-safe still needs the latest visible output for the CHAIN root.
        state.set("current_trace_response", response_str)

    def _stored_counter(suffix: str) -> int | None:
        raw = state.get(f"model_{model_call_id}_{suffix}_tokens")
        if raw is None:
            return None
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return None
        return value if value >= 0 else None

    p_tokens = _stored_counter("p")
    candidate_tokens = _stored_counter("c")
    reasoning_tokens = _stored_counter("r")
    total_tokens = _stored_counter("t")
    completion_tokens = (
        (candidate_tokens or 0) + (reasoning_tokens or 0)
        if candidate_tokens is not None or reasoning_tokens is not None
        else None
    )

    span_name = f"LLM: {model_name}" if model_name else "LLM"
    attrs = {
        "session.id": session_id,
        "project.name": project_name,
        "openinference.span.kind": "LLM",
        "llm.model_name": model_name,
        "input.value": redact_content(env.log_prompts, prompt_str),
        "output.value": redact_content(env.log_prompts, response_str),
    }
    if p_tokens is not None:
        attrs["llm.token_count.prompt"] = p_tokens
    if completion_tokens is not None:
        attrs["llm.token_count.completion"] = completion_tokens
    if total_tokens is not None:
        attrs["llm.token_count.total"] = total_tokens
    if reasoning_tokens is not None:
        attrs["llm.token_count.completion_details.reasoning"] = reasoning_tokens
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        span_name,
        "LLM",
        generate_span_id(),
        trace_id,
        parent_span_id,
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)
    _clear_model_state(state, model_call_id)


def _close_pending_turn(state, reason: str) -> None:
    """Emit a CHAIN root span for any pending turn, then clear trace state.

    Gemini does not always fire AfterAgent (cancellation, errors, slash commands),
    which leaves child LLM/TOOL spans pointing at a parent_span_id that was
    never sent. This fail-safe closes the dangling root so traces are connected
    in Arize. Called from both BeforeAgent (before starting a new turn) and
    SessionEnd. No-op if no pending trace state exists.
    """
    pending_trace_id = state.get("current_trace_id")
    pending_span_id = state.get("current_trace_span_id")
    if not pending_trace_id or not pending_span_id:
        # No turn in flight; still flush a stranded model call if any.
        _flush_pending_model_call(state)
        return

    # Close any in-flight model call as a child LLM span first, so its
    # parent_span_id remains valid before we tear down the turn.
    _flush_pending_model_call(state)

    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""
    start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    prompt = state.get("current_trace_prompt") or ""
    response = state.get("current_trace_response") or ""

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "project.name": project_name,
        "input.value": redact_content(env.log_prompts, prompt),
        "output.value": redact_content(env.log_prompts, response) if response else f"(closed by {reason} fail-safe)",
        "gemini.turn.close_reason": reason,
    }
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Turn",
        "CHAIN",
        pending_span_id,
        pending_trace_id,
        "",
        start_time,
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)

    state.delete("current_trace_id")
    state.delete("current_trace_span_id")
    state.delete("current_trace_start_time")
    state.delete("current_trace_prompt")
    state.delete("current_trace_response")


def _handle_session_start(input_json: dict) -> None:
    """Handle session start: initialize session."""
    debug_dump("gemini_session_start", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)
    session_id = state.get("session_id") or ""
    log(f"Session started: {session_id}")


def _handle_session_end(input_json: dict) -> None:
    """Handle session end: close pending turns, log summary, clean up."""
    debug_dump("gemini_session_end", input_json)
    state = resolve_session(input_json)
    session_id = state.get("session_id")
    if session_id is None:
        return

    _close_pending_turn(state, "SessionEnd")

    trace_count = state.get("trace_count") or "0"
    tool_count = state.get("tool_count") or "0"
    log(f"Session complete: {trace_count} traces, {tool_count} tools")

    # Clean up state file and lock
    if state.state_file is not None:
        state.state_file.unlink(missing_ok=True)
    if state._lock_path is not None:
        if state._lock_path.is_dir():
            try:
                state._lock_path.rmdir()
            except OSError:
                pass
        elif state._lock_path.is_file():
            try:
                state._lock_path.unlink(missing_ok=True)
            except OSError:
                pass

    gc_stale_state_files()


def _handle_before_agent(input_json: dict) -> None:
    """Handle before_agent: start of a turn."""
    debug_dump("gemini_before_agent", input_json)
    state = resolve_session(input_json)
    ensure_session_initialized(state, input_json)

    # If a previous turn is still pending (AfterAgent never fired -- cancel,
    # crash, slash command), close it as a CHAIN root before starting the
    # new turn. Otherwise child spans from the new turn would be created
    # under the prior turn's IDs and never get a root span.
    _close_pending_turn(state, "BeforeAgent")

    state.increment("trace_count")
    state.set("current_trace_id", generate_trace_id())
    state.set("current_trace_span_id", generate_span_id())
    state.set("current_trace_start_time", str(get_timestamp_ms()))
    state.delete("current_trace_response")

    # Extract prompt: real CLI uses flat 'prompt'
    prompt_obj = _get_robust(input_json, "prompt")
    if prompt_obj is not None:
        prompt_str = _extract_text(prompt_obj)
    else:
        messages = _get_robust(input_json, "messages", default=[])
        prompt_str = ""
        if isinstance(messages, list) and messages:
            last_user = next((m for m in reversed(messages) if m.get("role") == "user"), None)
            if last_user:
                prompt_str = _extract_text(_get_robust(last_user, "content", default=""))

    if not isinstance(prompt_str, str):
        prompt_str = json.dumps(prompt_str) if prompt_str else ""

    # Store RAW prompt in state; redact only at span build time.
    state.set("current_trace_prompt", prompt_str)


def _handle_after_agent(input_json: dict) -> None:
    """Handle after_agent: build the root CHAIN span for the completed turn."""
    debug_dump("gemini_after_agent", input_json)
    state = resolve_session(input_json)

    # Flush any model call still buffering streaming chunks before we close
    # the turn -- otherwise its accumulated text would be discarded.
    _flush_pending_model_call(state)

    trace_id = state.get("current_trace_id")
    span_id = state.get("current_trace_span_id")
    start_time = state.get("current_trace_start_time")
    prompt = state.get("current_trace_prompt") or ""

    if not trace_id or not span_id:
        return

    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""

    # Extract response: try prompt_response (CLI specific) then standard keys
    response_str = (
        _first_response_text(input_json, "prompt_response", "llm_response", "response", "model_response")
        or state.get("current_trace_response")
        or ""
    )

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "CHAIN",
        "project.name": project_name,
        "input.value": redact_content(env.log_prompts, prompt),
        "output.value": redact_content(env.log_prompts, response_str or ""),
    }
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Turn",
        "CHAIN",
        span_id,
        trace_id,
        "",
        start_time or str(get_timestamp_ms()),
        str(get_timestamp_ms()),
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)

    # Clear trace state
    state.delete("current_trace_id")
    state.delete("current_trace_span_id")
    state.delete("current_trace_start_time")
    state.delete("current_trace_prompt")
    state.delete("current_trace_response")


def _handle_before_model(input_json: dict) -> None:
    """Handle before_model: open a new model-call accumulator.

    Gemini does not pass model_call_id on AfterModel events, so we key
    accumulation off a generated id stashed in current_model_call_id.
    If a previous model call is still in flight (no final chunk arrived),
    flush it as its own span before opening the new one.
    """
    debug_dump("gemini_before_model", input_json)
    state = resolve_session(input_json)

    _flush_pending_model_call(state)

    model_call_id = _get_robust(input_json, "model_call_id") or generate_span_id()
    state.set(f"model_{model_call_id}_start", str(get_timestamp_ms()))
    state.set("current_model_call_id", model_call_id)

    req = _get_robust(input_json, "llm_request") or {}
    messages = _get_robust(req, "messages") or _get_robust(input_json, "messages", default=[])
    if isinstance(messages, list) and messages:
        state.set(f"model_{model_call_id}_prompt", json.dumps(messages))

    model_name = _get_robust(req, "model") or _get_robust(input_json, "model", "model_name", default="")
    if model_name:
        state.set(f"model_{model_call_id}_model", model_name)


def _handle_after_model(input_json: dict) -> None:
    """Handle after_model: accumulate one streaming chunk for the current call.

    Gemini may fire AfterModel for multiple streaming updates. Instead of
    emitting a span per update (the old behavior produced 4-30+ near-empty
    LLM rows per turn), accumulate text, usage, and model name in state.
    Flush immediately only on an explicit terminal marker; BeforeModel,
    AfterAgent, and the pending-turn fail-safe are authoritative fallback
    boundaries when captured payloads omit one.
    """
    debug_dump("gemini_after_model", input_json)
    state = resolve_session(input_json)

    trace_id = state.get("current_trace_id")
    if not trace_id:
        return

    model_call_id = _get_robust(input_json, "model_call_id") or state.get("current_model_call_id") or ""
    if not model_call_id:
        # AfterModel without a preceding BeforeModel -- start an accumulator
        # on the fly so the chunk's content isn't lost.
        model_call_id = generate_span_id()
        state.set(f"model_{model_call_id}_start", str(get_timestamp_ms()))
        state.set("current_model_call_id", model_call_id)

    # Capture model name from this chunk if not already set.
    req = _get_robust(input_json, "llm_request") or {}
    chunk_model = _get_robust(req, "model") or _get_robust(input_json, "model", "model_name", default="")
    if chunk_model and not state.get(f"model_{model_call_id}_model"):
        state.set(f"model_{model_call_id}_model", chunk_model)

    # Capture prompt from first chunk that includes messages. Subsequent
    # streaming chunks carry near-empty messages (just the partial response
    # so far) so we keep the first non-empty prompt we see.
    if not state.get(f"model_{model_call_id}_prompt"):
        messages = _get_robust(req, "messages") or _get_robust(input_json, "messages", default=[])
        if isinstance(messages, list) and messages:
            state.set(f"model_{model_call_id}_prompt", json.dumps(messages))

    # Append this chunk's recognized model-response text to the accumulator.
    chunk_text = _first_response_text(input_json, "llm_response", "response", "model_response")
    if chunk_text:
        prior = state.get(f"model_{model_call_id}_response") or ""
        state.set(f"model_{model_call_id}_response", prior + chunk_text)

    # Usage may arrive before the stream's terminal event. Accumulate it, but
    # close only on an explicit lifecycle marker; AfterAgent/BeforeModel are
    # the fallback boundaries when Gemini omits one.
    if _has_usage(input_json):
        p_tokens, c_tokens, reasoning_tokens, total_tokens = _extract_token_updates(input_json)
        if p_tokens is not None:
            state.set(f"model_{model_call_id}_p_tokens", str(p_tokens))
        if c_tokens is not None:
            state.set(f"model_{model_call_id}_c_tokens", str(c_tokens))
        if reasoning_tokens is not None:
            state.set(f"model_{model_call_id}_r_tokens", str(reasoning_tokens))
        if total_tokens is not None:
            state.set(f"model_{model_call_id}_t_tokens", str(total_tokens))
    if _is_model_final(input_json):
        _flush_pending_model_call(state)


def _handle_before_tool(input_json: dict) -> None:
    """Handle before_tool: record tool start time."""
    debug_dump("gemini_before_tool", input_json)
    state = resolve_session(input_json)

    pending_key, has_call_id = _tool_pending_key(input_json)
    start_time = str(get_timestamp_ms())
    if has_call_id:
        state.set(pending_key, start_time)
    else:
        _append_tool_start(state, pending_key, start_time)


def _handle_after_tool(input_json: dict) -> None:
    """Handle after_tool: build and send a TOOL span."""
    debug_dump("gemini_after_tool", input_json)
    state = resolve_session(input_json)

    trace_id = state.get("current_trace_id")
    parent_span_id = state.get("current_trace_span_id")
    if not trace_id or not parent_span_id:
        return

    session_id = state.get("session_id") or ""
    project_name = state.get("project_name") or ""
    user_id = state.get("user_id") or ""

    state.increment("tool_count")

    tool_name = _get_robust(input_json, "tool_name", default="unknown")
    pending_key, has_call_id = _tool_pending_key(input_json)

    tool_args_raw = _get_robust(input_json, "tool_input", "tool_args", "args") or {}
    tool_input = json.dumps(tool_args_raw) if isinstance(tool_args_raw, (dict, list)) else str(tool_args_raw)
    # Gemini CLI emits real tool output under `tool_response`, but the field
    # can be an empty dict on some events. Try `tool_response` first; only fall
    # through to `tool_result` / `result` when it's empty/None so we never lose
    # populated output to an empty wrapper.
    tool_output_raw = _get_robust(input_json, "tool_response")
    if not tool_output_raw:
        tool_output_raw = _get_robust(input_json, "tool_result", "result")
    tool_output = _extract_text(tool_output_raw)

    tool_command = ""
    tool_file_path = ""
    tool_url = ""
    tool_query = ""
    tool_description = ""

    if isinstance(tool_args_raw, dict):
        if tool_name == "run_shell_command":
            tool_command = _get_robust(tool_args_raw, "command", default="")
            tool_description = tool_command[:200]
        elif tool_name in ("read_file", "write_file", "replace", "edit"):
            tool_file_path = _get_robust(tool_args_raw, "file_path", "absolute_path", default="")
            tool_description = tool_file_path[:200]
        elif tool_name == "glob":
            tool_query = _get_robust(tool_args_raw, "pattern", default="")
            tool_file_path = _get_robust(tool_args_raw, "path", default="")
            tool_description = tool_query[:200]
        elif tool_name in ("search_file_content", "grep"):
            tool_query = _get_robust(tool_args_raw, "pattern", default="")
            tool_file_path = _get_robust(tool_args_raw, "path", default="")
            tool_description = f"grep: {tool_query[:100]}"
        elif tool_name == "web_fetch":
            tool_url = _get_robust(tool_args_raw, "url", default="")
            tool_description = tool_url[:200]
        elif tool_name in ("google_web_search", "web_search"):
            tool_query = _get_robust(tool_args_raw, "query", default="")
            tool_description = tool_query[:200]
        else:
            tool_description = tool_input[:200]
    else:
        tool_description = tool_input[:200]

    if has_call_id:
        start_time = state.get(pending_key)
        state.delete(pending_key)
    else:
        start_time = _pop_tool_start(state, pending_key)
    start_time = start_time or str(get_timestamp_ms())
    end_time = str(get_timestamp_ms())

    tool_input = redact_content(env.log_tool_content, tool_input)
    tool_output = redact_content(env.log_tool_content, tool_output)
    tool_description = redact_content(env.log_tool_details, tool_description)
    if tool_command:
        tool_command = redact_content(env.log_tool_details, tool_command)
    if tool_file_path:
        tool_file_path = redact_content(env.log_tool_details, tool_file_path)
    if tool_url:
        tool_url = redact_content(env.log_tool_details, tool_url)
    if tool_query:
        tool_query = redact_content(env.log_tool_details, tool_query)

    attrs = {
        "session.id": session_id,
        "openinference.span.kind": "TOOL",
        "project.name": project_name,
        "tool.name": tool_name,
        "input.value": tool_input,
        "output.value": tool_output,
        "tool.description": tool_description,
    }
    if user_id:
        attrs["user.id"] = user_id
    if tool_command:
        attrs["tool.command"] = tool_command
    if tool_file_path:
        attrs["tool.file_path"] = tool_file_path
    if tool_url:
        attrs["tool.url"] = tool_url
    if tool_query:
        attrs["tool.query"] = tool_query

    span = build_span(
        tool_name,
        "TOOL",
        generate_span_id(),
        trace_id,
        parent_span_id,
        start_time,
        end_time,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    _send_span_async(span)


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _run_hook(name: str, handler) -> None:
    """Read one event, serialize state mutation, then export outside the lock."""
    global _DEFERRED_SPANS

    input_json = {}
    deferred_spans: list[dict] = []
    _DEFERRED_SPANS = deferred_spans
    try:
        input_json = _read_stdin()
        if check_requirements():
            # Network export is deferred until this lock is released. The lock
            # therefore protects only the short state transaction, leaving
            # ample room inside Gemini's 30-second subprocess deadline.
            with FileLock(dispatch_lock_path(input_json), timeout=5.0, break_on_timeout=False):
                handler(input_json)
    except TimeoutError:
        error(f"gemini {name} hook timed out waiting for the session dispatch lock")
    except Exception as exc:
        error(f"gemini {name} hook failed: {exc}")
    finally:
        _DEFERRED_SPANS = None
        for span in deferred_spans:
            try:
                _export_span_async(span)
            except Exception as exc:
                error(f"gemini {name} hook failed to export span: {exc}")
        _print_response()


def session_start():
    """Entry point for arize-hook-gemini-session-start."""
    _run_hook("session_start", _handle_session_start)


def session_end():
    """Entry point for arize-hook-gemini-session-end."""
    _run_hook("session_end", _handle_session_end)


def before_agent():
    """Entry point for arize-hook-gemini-before-agent."""
    _run_hook("before_agent", _handle_before_agent)


def after_agent():
    """Entry point for arize-hook-gemini-after-agent."""
    _run_hook("after_agent", _handle_after_agent)


def before_model():
    """Entry point for arize-hook-gemini-before-model."""
    _run_hook("before_model", _handle_before_model)


def after_model():
    """Entry point for arize-hook-gemini-after-model."""
    _run_hook("after_model", _handle_after_model)


def before_tool():
    """Entry point for arize-hook-gemini-before-tool."""
    _run_hook("before_tool", _handle_before_tool)


def after_tool():
    """Entry point for arize-hook-gemini-after-tool."""
    _run_hook("after_tool", _handle_after_tool)


def main() -> None:
    """Manual execution dispatcher."""
    if len(sys.argv) < 2:
        print(f"usage: {sys.argv[0]} <handler_name>", file=sys.stderr)
        sys.exit(1)

    handler_name = sys.argv[1]
    handlers = {
        "session_start": session_start,
        "session_end": session_end,
        "before_agent": before_agent,
        "after_agent": after_agent,
        "before_model": before_model,
        "after_model": after_model,
        "before_tool": before_tool,
        "after_tool": after_tool,
    }

    handler = handlers.get(handler_name)
    if not handler:
        print(f"unknown handler: {handler_name}", file=sys.stderr)
        sys.exit(1)

    handler()


if __name__ == "__main__":
    main()
