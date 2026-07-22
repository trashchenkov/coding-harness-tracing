#!/usr/bin/env python3
"""Cursor hook handler: single entry point dispatching current Cursor hook events.

Replaces tracing/cursor/hooks/hook-handler.sh (475 lines).

Input contract: JSON on stdin, all registered events routed here.
stdout: MUST print permissive JSON response, even on error.
stderr: redirected to ARIZE_LOG_FILE before dispatch.
"""
import json
import os
import sys
from typing import Optional, Tuple

from core.common import build_span, env, error, get_timestamp_ms, log, redact_content
from core.common import send_span as _send_span_to_backend
from tracing.cursor.hooks.adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    check_requirements,
    completion_ledger_guard,
    gen_root_span_get,
    gen_root_span_save,
    generation_claim_terminal_event,
    generation_completion_status,
    generation_digest_completion_status,
    generation_finish_pending_cleanup_batch,
    generation_guard,
    generation_mark_cleanup_done,
    generation_mark_digest_completed,
    generation_pending_cleanup_batch,
    generation_state_key,
    generation_terminal_attribution_get,
    generation_terminal_attribution_note_usage,
    generation_terminal_event_is_claimable,
    span_id_16,
    stable_digest,
    state_claim_once,
    state_cleanup_generation,
    state_cleanup_generation_digest,
    state_complete_once,
    state_forget_completion,
    state_peek_all,
    state_pop,
    state_pop_matching,
    state_push,
    trace_id_from_generation,
    truncate_attr,
)

# ---------------------------------------------------------------------------
# Span send (with project.name injection)
# ---------------------------------------------------------------------------


def _resolve_project_name() -> str:
    """Project name for Cursor spans: framework env override or config.json,
    else cwd basename, else the service name.

    Cursor builds spans across the current hook inventory and keeps no per-session
    project state, so it resolves the project centrally at send time — matching
    the framework-scoped resolution the other harnesses do in their adapters.
    """
    return env.project_name_for(SERVICE_NAME) or os.path.basename(os.getcwd()) or SERVICE_NAME


def send_span(payload: dict) -> bool:
    """Inject ``project.name`` onto every span in the payload, then send.

    Wraps ``core.common.send_span`` so all handler send sites get the attribute
    without threading project name through each attrs dict.
    """
    project_name = _resolve_project_name()
    attr = {"key": "project.name", "value": {"stringValue": project_name}}
    for rs in payload.get("resourceSpans", []):
        for ss in rs.get("scopeSpans", []):
            for span in ss.get("spans", []):
                span.setdefault("attributes", []).append(attr)

    def bound_string_values(value):
        """Bound every OTLP stringValue immediately before transport."""
        if isinstance(value, dict):
            if isinstance(value.get("stringValue"), str):
                value["stringValue"] = truncate_attr(value["stringValue"])
            for child in value.values():
                bound_string_values(child)
        elif isinstance(value, list):
            for child in value:
                bound_string_values(child)

    bound_string_values(payload)
    return _send_span_to_backend(payload)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_permissive(event: str) -> None:
    """Print an event-valid fail-open JSON response to stdout.

    Cursor validates response fields per event. Permission-gated hooks use
    ``permission=allow``; ``beforeSubmitPrompt`` has its own ``continue``
    control; observational hooks accept an empty object.

    Uses sys.__stdout__ (the original stdout saved by Python) in case
    sys.stdout has been redirected.
    """
    stdout = sys.__stdout__ or sys.stdout
    permission_events = {
        "beforeShellExecution",
        "beforeMCPExecution",
        "beforeReadFile",
        "beforeTabFileRead",
        "preToolUse",
        "subagentStart",
    }
    response: dict[str, object]
    if event in permission_events:
        response = {"permission": "allow"}
    elif event == "beforeSubmitPrompt":
        response = {"continue": True}
    elif event == "workspaceOpen":
        response = {"pluginPaths": []}
    else:
        response = {}
    stdout.write(json.dumps(response, separators=(",", ":")))
    stdout.flush()


def _jq_str(input_json: dict, *keys, default: str = "") -> str:
    """Try multiple keys in order, return first non-None/non-empty string value.

    Matches bash: echo "$INPUT" | jq -r "$1" 2>/dev/null || echo "${2:-}"
    """
    for key in keys:
        val = input_json.get(key)
        if val is not None and val != "":
            return str(val)
    return default


def _json_string(value) -> str:
    """Serialize structured hook fields deterministically for text attributes."""
    if value is None or value == "":
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return str(value)


def _redact_deferred(allowed_now: bool, value: str, was_redacted: bool) -> str:
    """Re-apply current policy without ever reversing creation-time redaction."""
    if was_redacted:
        return value or ""
    return redact_content(allowed_now, value)


def _redact_terminal_field(privacy_key: str, field: str, raw_value: str, allowed_now: bool) -> tuple[str, bool]:
    """Retain a terminal payload field's first-delivery privacy decision."""
    privacy_state = state_pop(privacy_key) or {}
    redacted_field = f"{field}_redacted"
    was_redacted = bool(privacy_state.get(redacted_field, False))
    source = privacy_state.get(field, "") if was_redacted else raw_value
    value = _redact_deferred(allowed_now, source, was_redacted)
    is_redacted = was_redacted or not allowed_now
    privacy_state[field] = value if is_redacted else ""
    privacy_state[redacted_field] = is_redacted
    state_push(privacy_key, privacy_state)
    return value, is_redacted


def _resolve_user_id(input_json: dict) -> str:
    """env.get_user_id(SERVICE_NAME) (global config < harnesses.cursor.user_id < ARIZE_USER_ID env)
    > payload `user_email` > "".

    Cursor has no per-session state for user_id, so each handler resolves it
    inline. Configured user_id wins over the implicit `user_email` payload field
    so an explicitly set user takes precedence on shared workstations.
    """
    return env.get_user_id(SERVICE_NAME) or _jq_str(input_json, "user_email")


def _to_int(v):
    """Coerce *v* to int if possible; return None for None, empty, or ``"--"``."""
    try:
        return int(v) if v not in (None, "", "--") else None
    except (TypeError, ValueError):
        return None


def _to_token_int(value):
    """Accept only non-negative integral token counters (numeric strings included)."""
    if isinstance(value, bool) or value in (None, "", "--"):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        converted = int(value)
    except (TypeError, ValueError):
        return None
    return converted if converted >= 0 else None


def _event_name(input_json: dict) -> str:
    """Extract event name, preferring Cursor's documented snake_case key.

    Other keys are retained only for compatibility with old/synthetic callers.
    """
    return _jq_str(input_json, "hook_event_name", "hookEventName", "event_name", "eventName", "event")


def _trace_id_from_event(gen_id: str, conversation_id: str) -> str:
    """Derive a trace ID from generation or conversation ID.

    Prefers gen_id; falls back to conversation_id for CLI events that may
    lack a generation_id.
    """
    if gen_id:
        return trace_id_from_generation(gen_id)
    if conversation_id:
        return trace_id_from_generation(conversation_id)
    return ""


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


def _sweep_pending_generation_cleanups() -> bool:
    """Retry durable cleanup work; return false to suppress unsafe dispatch."""
    with completion_ledger_guard():
        batch = generation_pending_cleanup_batch()
        if batch is None:
            log("Pending generation cleanup ledger unavailable; suppressing dispatch")
            return False
        digests, has_more = batch
        if not digests:
            return True
        failed = []
        for digest in digests:
            try:
                state_cleanup_generation_digest(digest)
            except Exception as exc:
                failed.append(digest)
                log(f"Pending generation cleanup failed for digest={digest}: {exc}")
        try:
            # One bounded transaction deletes only this batch's successful
            # markers; failures and any unselected backlog remain durable.
            generation_finish_pending_cleanup_batch(digests, failed)
        except Exception as exc:
            log(f"Pending generation cleanup ledger update failed: {exc}")
            return False
        return not failed and not has_more


def _dispatch(event: str, input_json: dict, *, sweep_pending: bool = True) -> None:
    """Route event to the appropriate handler."""
    conversation_id = input_json.get("conversation_id", "")
    gen_id = input_json.get("generation_id", "")

    # Privacy cleanup remains live even if tracing is disabled after a terminal
    # claim, and runs before any new event state can be handled.
    if sweep_pending and not _sweep_pending_generation_cleanups():
        return

    # Early exit: tracing disabled
    if not env.trace_enabled:
        return

    trace_id = _trace_id_from_event(gen_id, conversation_id)
    now_ms = get_timestamp_ms()

    handlers = {
        "beforeSubmitPrompt": _handle_before_submit_prompt,
        "afterAgentResponse": _handle_after_agent_response,
        "afterAgentThought": _handle_after_agent_thought,
        "beforeShellExecution": _handle_before_shell_execution,
        "afterShellExecution": _handle_after_shell_execution,
        "beforeMCPExecution": _handle_before_mcp_execution,
        "afterMCPExecution": _handle_after_mcp_execution,
        "beforeReadFile": _handle_before_read_file,
        "afterFileEdit": _handle_after_file_edit,
        "beforeTabFileRead": _handle_before_tab_file_read,
        "afterTabFileEdit": _handle_after_tab_file_edit,
        "stop": _handle_stop,
        "sessionStart": _handle_session_start,
        "sessionEnd": _handle_session_end,
        "preToolUse": _handle_pre_tool_use,
        "postToolUse": _handle_post_tool_use,
        "postToolUseFailure": _handle_post_tool_use_failure,
        "subagentStart": _handle_subagent_start,
        "subagentStop": _handle_subagent_stop,
        "preCompact": _handle_pre_compact,
        "workspaceOpen": _handle_workspace_open,
    }

    handler = handlers.get(event)
    if not handler:
        log(f"Unknown hook event: {event}")
        return

    if not gen_id:
        with completion_ledger_guard():
            if event in {"stop", "sessionEnd"}:
                if not conversation_id:
                    log(f"Ignoring {event} without generation or conversation identity")
                    return
                legacy_digest = stable_digest(f"cursor-terminal-fallback\0{conversation_id}")
                if generation_digest_completion_status(legacy_digest) != "active":
                    log(f"Ignoring {event} with legacy generationless completion")
                    return
                fallback_digest = stable_digest(f"cursor-terminal-fallback\0{event}\0{conversation_id}")
                completion_status = generation_digest_completion_status(fallback_digest)
                if completion_status != "active":
                    log(f"Ignoring {event} without generation: {completion_status}")
                    return
                delivery_key = f"terminalfallback_{fallback_digest}"
                _, delivered = state_complete_once(
                    delivery_key,
                    lambda: handler(input_json, conversation_id, gen_id, trace_id, now_ms),
                )
                if not delivered:
                    log(f"Retaining retryable generationless {event} after transport failure")
                    return
                # Bridge transport success to the durable claim without replay:
                # if the ledger write fails, retry sees this raw-free marker and
                # commits the claim without resending the terminal span.
                generation_mark_digest_completed(fallback_digest)
                state_forget_completion(delivery_key)
                return
            else:
                completion_status = generation_completion_status("")
                if completion_status != "active":
                    log(f"Ignoring {event} without generation: {completion_status}")
                    return
            handler(input_json, conversation_id, gen_id, trace_id, now_ms)
        return

    # Serialize terminal delivery attempts, and keep the global ledger guard
    # across check → send → claim so a successful transport cannot race another
    # process into a duplicate claim.
    with generation_guard(gen_id):
        with completion_ledger_guard():
            if event in {"stop", "sessionEnd"}:
                if not generation_terminal_event_is_claimable(gen_id, event):
                    log(f"Ignoring duplicate or unclaimable {event} for generation")
                    return
                delivery_key = f"terminalsend_{generation_state_key(gen_id)}_{event}"
                _, delivered = state_complete_once(
                    delivery_key,
                    lambda: handler(input_json, conversation_id, gen_id, trace_id, now_ms),
                )
                if not delivered:
                    log(f"Retaining retryable {event} state after transport failure")
                    return
                if not generation_claim_terminal_event(gen_id, event):
                    log(f"Unable to record delivered {event} for generation")
                    return
                state_forget_completion(delivery_key)
                state_cleanup_generation(gen_id)
                generation_mark_cleanup_done(stable_digest(gen_id))
            else:
                completion_status = generation_completion_status(gen_id)
                if completion_status != "active":
                    log(f"Ignoring {event} for generation: {completion_status}")
                    return
                handler(input_json, conversation_id, gen_id, trace_id, now_ms)


# ---------------------------------------------------------------------------
# Event handlers
# ---------------------------------------------------------------------------


def _handle_before_submit_prompt(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Create the turn root, deferred when a generation can pair lifecycle events.

    Cursor's documented and current CLI payloads both use ``hook_event_name``;
    there is no supported host discriminator. A missing generation cannot be
    paired safely, so that degraded case is emitted immediately without state.
    """
    sid = span_id_16()
    if gen_id:
        gen_root_span_save(gen_id, sid)

    prompt_allowed = env.log_prompts
    prompt = redact_content(prompt_allowed, _jq_str(input_json, "prompt", "input", "text"))
    model = _jq_str(input_json, "model", "model_name")
    deferred_root = bool(gen_id)

    if gen_id:
        state_push(
            f"root_{generation_state_key(gen_id)}",
            {
                "span_id": sid,
                "trace_id": trace_id,
                "conversation_id": conversation_id,
                "start_ms": now_ms,
                "prompt": prompt,
                "prompt_redacted": not prompt_allowed,
                "model": model,
                "deferred_root": deferred_root,
            },
        )

    if deferred_root:
        log(f"beforeSubmitPrompt: deferred root span {sid} (trace={trace_id})")
        return

    user_id = _resolve_user_id(input_json)

    root_attrs = {
        "openinference.span.kind": "CHAIN",
        "input.value": prompt,
        "session.id": conversation_id,
    }
    if conversation_id:
        root_attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        root_attrs["user.id"] = user_id
    if model:
        root_attrs["llm.model_name"] = model

    root_span = build_span(
        "User Prompt",
        "CHAIN",
        sid,
        trace_id,
        "",
        now_ms,
        now_ms,
        root_attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(root_span)
    log(f"beforeSubmitPrompt: root span {sid} (trace={trace_id})")


def _handle_after_agent_response(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Defer the LLM span until stop so per-turn tokens land on it."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    # "text" is the documented field; fall back to "response"/"output" for compat
    raw_response = _jq_str(input_json, "text", "response", "output")
    # "model" is a base field on all hook events
    model = _jq_str(input_json, "model", "model_name")

    safe_gen = generation_state_key(gen_id) if gen_id else ""
    root_state = state_pop(f"root_{safe_gen}") if safe_gen else None
    prompt = root_state.get("prompt", "") if root_state else ""
    deferred_root = root_state.get("deferred_root", True) if root_state else True

    # Prompt and model output have independent privacy controls. A response hash
    # identifies duplicate delivery without persisting raw model content.
    prompt_was_redacted = bool(root_state and root_state.get("prompt_redacted", False))
    prompt = _redact_deferred(env.log_prompts, prompt, prompt_was_redacted)
    response_privacy_key = ""
    response_privacy = None
    if safe_gen:
        response_hash = stable_digest(raw_response)[:24]
        response_privacy_key = f"llm_privacy_{safe_gen}_{response_hash}"
        response_privacy = state_pop(response_privacy_key)
    if response_privacy:
        response_source = response_privacy.get("output", "")
        response_was_redacted = bool(response_privacy.get("output_redacted", False))
    else:
        response_source = raw_response
        response_was_redacted = False
    response = _redact_deferred(env.log_model_outputs, response_source, response_was_redacted)
    response_is_redacted = response_was_redacted or not env.log_model_outputs
    if response_privacy_key:
        state_push(
            response_privacy_key,
            {"output": response if response_is_redacted else "", "output_redacted": response_is_redacted},
        )
    prompt_is_redacted = prompt_was_redacted or not env.log_prompts

    user_id = _resolve_user_id(input_json)

    # Send User Prompt CHAIN first (parent before LLM for strict backends), full I/O + duration.
    if root_state and deferred_root:
        root_conv_id = root_state.get("conversation_id", conversation_id)
        root_attrs = {
            "openinference.span.kind": "CHAIN",
            "input.value": prompt,
            "output.value": response,
            "session.id": root_conv_id,
        }
        if root_conv_id:
            root_attrs["cursor.conversation.id"] = root_conv_id
        if user_id:
            root_attrs["user.id"] = user_id
        root_model = model or root_state.get("model", "")
        if root_model:
            root_attrs["llm.model_name"] = root_model

        root_span = build_span(
            "User Prompt",
            "CHAIN",
            root_state["span_id"],
            root_state.get("trace_id", trace_id),
            "",
            root_state.get("start_ms", now_ms),
            now_ms,
            root_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
        send_span(root_span)
        log(f"afterAgentResponse: sent deferred root span {root_state['span_id']}")

    llm_entry = {
        "span_id": sid,
        "parent": parent,
        "trace_id": trace_id,
        "input": prompt,
        "output": response,
        "input_redacted": prompt_is_redacted,
        "output_redacted": response_is_redacted,
        "model": model,
        "conversation_id": conversation_id,
        "user_id": user_id,
        "start_ms": now_ms,
    }

    if gen_id:
        # Defer LLM span to stop; tokens (only available at stop) attach there.
        state_push(f"llm_{generation_state_key(gen_id)}", llm_entry)
        log(f"afterAgentResponse: deferred LLM span {sid}")
        return

    # Fallback: no gen_id means we have no key to stash under — send inline.
    attrs = {
        "openinference.span.kind": "LLM",
        "input.value": prompt,
        "output.value": response,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if model:
        attrs["llm.model_name"] = model

    span = build_span(
        "Agent Response",
        "LLM",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"afterAgentResponse: child span {sid} (no gen_id, sent inline)")


def _handle_after_agent_thought(input_json, conversation_id, gen_id, trace_id, now_ms):
    """CHAIN span for thinking. Replaces bash lines 138-158."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    thought = _jq_str(input_json, "thought", "thinking", "text")

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "CHAIN",
        "output.value": redact_content(env.log_model_outputs, thought),
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Agent Thinking",
        "CHAIN",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"afterAgentThought: span {sid}")


def _handle_before_shell_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """State push only, no span. Replaces bash lines 163-179."""
    if not gen_id:
        return

    command = _jq_str(input_json, "command", "shell_command")
    cwd = _jq_str(input_json, "cwd", "working_directory")
    command_allowed = env.log_tool_details

    correlation = stable_digest(command)
    state_push(
        f"shell_{generation_state_key(gen_id)}",
        {
            "correlation": correlation,
            "command": redact_content(command_allowed, command),
            "command_redacted": not command_allowed,
            "cwd": cwd,
            "start_ms": str(now_ms),
            "trace_id": trace_id,
            "conversation_id": conversation_id,
        },
    )
    log(f"beforeShellExecution: pushed state for gen={gen_id}")


def _handle_after_shell_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Merge with before state, create TOOL span. Replaces bash lines 184-232."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)
    shell_key = f"shell_{generation_state_key(gen_id)}" if gen_id else ""
    after_cmd = _jq_str(input_json, "command", "shell_command")
    correlation = stable_digest(after_cmd) if after_cmd else ""
    privacy_key = f"shell_privacy_{generation_state_key(gen_id)}_{correlation}" if gen_id and correlation else ""
    if shell_key and correlation:
        popped = state_pop_matching(shell_key, "correlation", correlation)
    else:
        # No identity means shell pairing is ambiguous; retain the historical
        # LIFO fallback, but do not borrow another invocation's tombstone.
        popped = state_pop(shell_key) if shell_key else None
    privacy_state = state_pop(privacy_key) if privacy_key else None

    if popped:
        start_ms = popped.get("start_ms", "")
        command = popped.get("command", "")
        command_was_redacted = bool(popped.get("command_redacted", False))
    else:
        start_ms = ""
        command = privacy_state.get("command", "") if privacy_state else ""
        command_was_redacted = bool(privacy_state and privacy_state.get("command_redacted", False))

    # Keep raw-content-free privacy provenance for duplicate terminal
    # deliveries. A redaction marker may be retained; the command may not.
    if privacy_key:
        privacy_record = dict(privacy_state or {})
        privacy_record["command_redacted"] = command_was_redacted
        privacy_record["command"] = command if command_was_redacted else ""
        state_push(privacy_key, privacy_record)
    start_ms = start_ms or str(now_ms)

    # Use the after-event command only when creation-time state did not redact
    # it. A terminal payload must not reverse an earlier privacy decision.
    if after_cmd and not command_was_redacted:
        command = after_cmd

    raw_output = _jq_str(input_json, "output", "stdout", "result")
    output = (
        _redact_terminal_field(privacy_key, "output", raw_output, env.log_tool_content)[0]
        if privacy_key
        else redact_content(env.log_tool_content, raw_output)
    )
    exit_code = _jq_str(input_json, "exit_code", "exitCode")

    command = _redact_deferred(env.log_tool_details, command, command_was_redacted)

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "shell",
        "input.value": command,
        "output.value": output,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if exit_code:
        attrs["shell.exit_code"] = exit_code

    span = build_span(
        "Shell",
        "TOOL",
        sid,
        trace_id,
        parent,
        start_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    sent = send_span(span)
    if sent and gen_id and correlation:
        state_push(
            _dedicated_reported_key(gen_id, "shell", correlation, _mcp_outcome_digest(False, raw_output)),
            {"reported": True},
        )
    log(f"afterShellExecution: span {sid} (merged)")


def _handle_before_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """State push only, no span. Replaces bash lines 237-257."""
    if not gen_id:
        return

    tool_name = _jq_str(input_json, "tool_name", "toolName", "name")
    tool_input = _jq_str(input_json, "tool_input", "toolInput", "input", "arguments")
    mcp_url = _jq_str(input_json, "url", "server_url", "serverUrl")
    mcp_cmd = _jq_str(input_json, "command")
    tool_content_allowed = env.log_tool_content
    state_key = _mcp_pair_state_key(gen_id)

    state_push(
        state_key,
        {
            # Stamped so the after-event can claim *this* record instead of
            # whatever is on top of the stack. Derived here, where the
            # arguments are still raw, and stored only as a digest.
            "correlation": _mcp_correlation_digest(tool_name, tool_input),
            "tool_name": tool_name,
            "tool_input": redact_content(tool_content_allowed, tool_input),
            "tool_input_redacted": not tool_content_allowed,
            "url": redact_content(env.log_tool_details, mcp_url),
            "command": redact_content(env.log_tool_details, mcp_cmd),
            "start_ms": str(now_ms),
            "trace_id": trace_id,
            "conversation_id": conversation_id,
        },
    )
    log(f"beforeMCPExecution: pushed state for gen={gen_id}")


def _handle_after_mcp_execution(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Merge with before state, create TOOL span. Replaces bash lines 262-312."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)
    after_tool = _jq_str(input_json, "tool_name", "toolName", "name")
    after_input = input_json.get("tool_input")
    if after_input is None:
        after_input = _jq_str(input_json, "toolInput", "input", "arguments")
    state_key = _mcp_pair_state_key(gen_id) if gen_id else ""

    # This payload names the call itself, so its own before-record is claimed
    # by content: two calls in flight at once cannot adopt each other's
    # arguments and start time. When it names the call but no record matches,
    # the before-event was lost — taking whatever is on the stack would steal
    # a *different* call's record, so nothing is taken. Only a payload that
    # identifies nothing falls back to arrival-order pairing.
    has_after_identity = bool(after_tool) and after_input not in (None, "")
    popped = None
    if state_key:
        if has_after_identity:
            popped = state_pop_matching(state_key, "correlation", _mcp_correlation_digest(after_tool, after_input))
        else:
            popped = state_pop(state_key)

    if popped:
        start_ms = popped.get("start_ms", "")
        tool_name = popped.get("tool_name", "")
    else:
        start_ms = ""
        tool_name = ""
    correlation = _mcp_correlation_digest(after_tool, after_input) if has_after_identity else ""
    privacy_state_key = f"{state_key}_{correlation}" if state_key and correlation else state_key
    tool_input, tool_input_redacted = (
        _tool_input_with_privacy_provenance(privacy_state_key, popped) if privacy_state_key else ("", False)
    )
    # Without a before-record the after payload's own arguments are the only
    # description of the call, and the elapsed time is unknown.
    if popped is None and has_after_identity and not tool_input and not tool_input_redacted:
        tool_input = redact_content(env.log_tool_content, _json_string(after_input))
    start_ms = start_ms or str(now_ms)

    # Override tool name from after-event if present
    if after_tool:
        tool_name = after_tool
    tool_name = tool_name or "unknown"

    raw_result = _jq_str(input_json, "result", "output", "result_json")
    result = (
        _redact_terminal_field(f"{privacy_state_key}_privacy", "result", raw_result, env.log_tool_content)[0]
        if privacy_state_key
        else redact_content(env.log_tool_content, raw_result)
    )

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": tool_name,
        "input.value": tool_input,
        "output.value": result,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    # If the after payload declares a failure, the dedicated span is the only
    # span for this call (the generic follow-up is suppressed), so it must
    # carry the error itself.
    raw_error = _jq_str(input_json, "error", "error_message", "errorMessage")
    error_message = redact_content(env.log_tool_content, raw_error)
    if raw_error:
        attrs["cursor.tool.status"] = "error"

    span = build_span(
        f"MCP: {tool_name}",
        "TOOL",
        sid,
        trace_id,
        parent,
        start_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
        status_code=2 if raw_error else 1,
        status_message=truncate_attr(error_message, 256) if raw_error else "",
    )
    sent = send_span(span)
    if sent and gen_id and after_tool:
        # Record that this exact call — arguments and result — has a span. The
        # generic follow-up decides for itself whether it is the same call, by
        # matching its own result against this record. Nothing is suppressed
        # here, so a dedicated pair can never silence an unrelated invocation.
        #
        # Only a confirmed export records anything: the marker asserts that a
        # span exists, and a backend that reported failure did not create one.
        # Leaving it unwritten lets the generic completion act as the fallback
        # it naturally is, rather than being suppressed by a span that was
        # never delivered.
        state_push(
            _mcp_dedicated_reported_key(
                gen_id,
                _mcp_correlation_digest(after_tool, after_input),
                _mcp_outcome_digest(bool(raw_error), raw_error or raw_result),
            ),
            {"reported": True},
        )
    log(f"afterMCPExecution: span {sid} (merged, tool={tool_name})")


def _handle_before_read_file(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for file read. Replaces bash lines 317-339."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "read_file",
        "input.value": file_path,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Read File",
        "TOOL",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"beforeReadFile: span {sid}")


def _handle_after_file_edit(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for file edit. Replaces bash lines 344-371."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))
    edits = redact_content(env.log_tool_content, _jq_str(input_json, "edits", "changes", "diff"))
    input_val = f"{file_path}: {edits}" if edits else file_path

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "edit_file",
        "input.value": input_val,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "File Edit",
        "TOOL",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"afterFileEdit: span {sid}")


def _handle_before_tab_file_read(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for tab file read. Replaces bash lines 376-398."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "read_file_tab",
        "input.value": file_path,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Tab Read File",
        "TOOL",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"beforeTabFileRead: span {sid}")


def _handle_after_tab_file_edit(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for tab file edit. Replaces bash lines 403-430."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    file_path = redact_content(env.log_tool_details, _jq_str(input_json, "file_path", "filePath", "path"))
    edits = redact_content(env.log_tool_content, _jq_str(input_json, "edits", "changes", "diff"))
    input_val = f"{file_path}: {edits}" if edits else file_path

    user_id = _resolve_user_id(input_json)

    attrs = {
        "openinference.span.kind": "TOOL",
        "tool.name": "edit_file_tab",
        "input.value": input_val,
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Tab File Edit",
        "TOOL",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"afterTabFileEdit: span {sid}")


def _terminal_attribution_context(gen_id):
    """Return (parent_span_id, usage_already_attributed) for a terminal event.

    Generation state — including the root span file — is deleted after the
    first terminal event, but ``stop`` and ``sessionEnd`` are distinct
    once-only events that can both arrive. The first terminal claim persists
    the root span identity and a usage flag in the completion ledger, so the
    second terminal event can still parent correctly and cumulative token
    counts are attributed exactly once per generation.
    """
    attribution = generation_terminal_attribution_get(gen_id) if gen_id else None
    parent = gen_root_span_get(gen_id)
    if not parent and attribution:
        parent = attribution["root_span_id"]
    return parent, bool(attribution and attribution["usage_attributed"])


def _flush_deferred_llm_spans(gen_id, trace_id, now_ms, token_attrs):
    """Drain and emit every deferred Agent Response span for a generation.

    Dispatch removes all generation state after either terminal event, so
    whichever of ``stop``/``sessionEnd`` arrives first must flush the deferred
    stack — otherwise a sessionEnd-first ordering would delete the pending
    entries before stop could emit them. Returns the flushed entries.
    """
    llm_key = f"llm_{generation_state_key(gen_id)}" if gen_id else ""
    llm_entries = list(reversed(state_peek_all(llm_key))) if llm_key else []
    sent_entries = []

    for idx, entry in enumerate(llm_entries):
        entry_conv_id = entry.get("conversation_id")
        llm_attrs = {
            "openinference.span.kind": "LLM",
            "input.value": _redact_deferred(
                env.log_prompts, entry.get("input", ""), bool(entry.get("input_redacted", False))
            ),
            "output.value": _redact_deferred(
                env.log_model_outputs, entry.get("output", ""), bool(entry.get("output_redacted", False))
            ),
        }
        if entry_conv_id:
            llm_attrs["session.id"] = entry_conv_id
            llm_attrs["cursor.conversation.id"] = entry_conv_id
        entry_user = entry.get("user_id")
        if entry_user:
            llm_attrs["user.id"] = entry_user
        entry_model = entry.get("model", "")
        if entry_model:
            llm_attrs["llm.model_name"] = entry_model
        # Tokens are cumulative per turn — attribute only to the most recent LLM span.
        if idx == 0:
            llm_attrs.update(token_attrs)

        llm_start = int(entry.get("start_ms") or now_ms)
        llm_span = build_span(
            "Agent Response",
            "LLM",
            entry.get("span_id", ""),
            entry.get("trace_id", trace_id),
            entry.get("parent", ""),
            llm_start,
            llm_start,
            llm_attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
        if not send_span(llm_span):
            return sent_entries, False
        state_pop(llm_key)
        sent_entries.append(entry)
        if idx == 0 and any(key.startswith("llm.token_count.") for key in token_attrs):
            generation_terminal_attribution_note_usage(gen_id)

    return sent_entries, True


def _handle_stop(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Flush deferred LLM span(s) with per-turn tokens, then send Agent Stop CHAIN + cleanup."""
    sid = span_id_16()
    parent, usage_attributed = _terminal_attribution_context(gen_id)

    status = _jq_str(input_json, "status", "reason")
    loop_count = _jq_str(input_json, "loop_count", "loopCount", "iterations")

    user_id = _resolve_user_id(input_json)

    # Token counts from stop payload
    # Use explicit None checks — 0 is a valid token count but falsy with ``or``
    _inp_tok = input_json.get("input_tokens")
    prompt_tokens = _to_token_int(_inp_tok if _inp_tok is not None else input_json.get("inputTokens"))
    _out_tok = input_json.get("output_tokens")
    completion_tokens = _to_token_int(_out_tok if _out_tok is not None else input_json.get("outputTokens"))
    _cr_tok = input_json.get("cache_read_tokens")
    cache_read = _to_token_int(_cr_tok if _cr_tok is not None else input_json.get("cacheReadTokens"))
    _cw_tok = input_json.get("cache_write_tokens")
    cache_write = _to_token_int(_cw_tok if _cw_tok is not None else input_json.get("cacheWriteTokens"))
    model = _jq_str(input_json, "model")
    _dur = input_json.get("duration_ms")
    duration_ms = _to_int(_dur if _dur is not None else input_json.get("durationMs"))

    # OpenInference: ``prompt`` is the total prompt. Cursor's ``input_tokens`` is
    # the uncached remainder (mirrors Anthropic), so the cache buckets are added
    # back in to form the total; they are also reported via ``prompt_details.*``
    # subsets so a cost model prices cache reads (~0.1x) and writes (~1.25x) at
    # their own rates instead of the full input rate.
    token_attrs = {}
    prompt_total = None
    if prompt_tokens is not None:
        prompt_total = prompt_tokens + (cache_read or 0) + (cache_write or 0)
        token_attrs["llm.token_count.prompt"] = prompt_total
    if completion_tokens is not None:
        token_attrs["llm.token_count.completion"] = completion_tokens
    if cache_read is not None:
        token_attrs["llm.token_count.prompt_details.cache_read"] = cache_read
    if cache_write is not None:
        token_attrs["llm.token_count.prompt_details.cache_write"] = cache_write
    if prompt_total is not None and completion_tokens is not None:
        token_attrs["llm.token_count.total"] = prompt_total + completion_tokens
    if model:
        token_attrs["llm.model_name"] = model

    # Flush deferred LLM span(s) before Agent Stop so strict OTLP backends see
    # parent first. Cumulative usage is attributed at most once per generation:
    # a terminal event that arrives after usage was already attributed carries
    # the same counts again and must not re-attach them anywhere.
    llm_entries, flush_complete = _flush_deferred_llm_spans(
        gen_id, trace_id, now_ms, {} if usage_attributed else token_attrs
    )
    if not flush_complete:
        return False

    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if status:
        attrs["cursor.stop.status"] = status
    if loop_count:
        attrs["cursor.stop.loop_count"] = loop_count
    if duration_ms is not None:
        attrs["cursor.stop.duration_ms"] = duration_ms
    if model and not llm_entries:
        attrs["llm.model_name"] = model

    # Fallback (no afterAgentResponse, e.g. CLI): keep token attrs on Agent Stop.
    if not usage_attributed and not llm_entries:
        attrs.update(token_attrs)
    span = build_span(
        "Agent Stop",
        "CHAIN",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    sent = send_span(span)
    if (
        sent
        and not usage_attributed
        and not llm_entries
        and any(key.startswith("llm.token_count.") for key in token_attrs)
    ):
        generation_terminal_attribution_note_usage(gen_id)
    log(f"stop: span {sid}, gen={gen_id}")
    return sent


def _handle_session_start(input_json, conversation_id, gen_id, trace_id, now_ms):
    """CHAIN span for Cursor CLI sessionStart event."""
    sid = span_id_16()

    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id

    cwd = _jq_str(input_json, "cwd", "workspace_root")
    if cwd:
        attrs["cursor.session.cwd"] = cwd

    user_id = _resolve_user_id(input_json)
    if user_id:
        attrs["user.id"] = user_id

    span = build_span(
        "Session Start",
        "CHAIN",
        sid,
        trace_id,
        "",
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)

    if gen_id:
        gen_root_span_save(gen_id, sid)

    log(f"sessionStart: span {sid} (trace={trace_id})")


def _handle_session_end(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Flush deferred LLM span(s), then send Session End CHAIN — closes the session.

    Reuses tokens/duration from the payload when present.  Cleanup is owned by
    the dispatcher's terminal claim/finally lifecycle, which runs after either
    terminal event — so a sessionEnd that precedes stop must flush the deferred
    stack itself or the entries would be deleted before stop could emit them.
    """
    sid = span_id_16()
    parent, usage_attributed = _terminal_attribution_context(gen_id)

    _dur = input_json.get("duration_ms")
    duration_ms = _to_int(_dur if _dur is not None else input_json.get("durationMs"))
    final_status = _jq_str(input_json, "final_status", "finalStatus", "status")
    reason = _jq_str(input_json, "reason")
    model = _jq_str(input_json, "model")

    user_id = _resolve_user_id(input_json)

    # Token fields can also appear on sessionEnd. Same OpenInference convention
    # as _handle_stop: ``prompt`` is the total (uncached input + cache buckets),
    # cache split reported via ``prompt_details.*`` subsets.
    _inp_tok = input_json.get("input_tokens")
    prompt_tokens = _to_token_int(_inp_tok if _inp_tok is not None else input_json.get("inputTokens"))
    _out_tok = input_json.get("output_tokens")
    completion_tokens = _to_token_int(_out_tok if _out_tok is not None else input_json.get("outputTokens"))
    _cr_tok = input_json.get("cache_read_tokens")
    cache_read = _to_token_int(_cr_tok if _cr_tok is not None else input_json.get("cacheReadTokens"))
    _cw_tok = input_json.get("cache_write_tokens")
    cache_write = _to_token_int(_cw_tok if _cw_tok is not None else input_json.get("cacheWriteTokens"))
    token_attrs = {}
    prompt_total = None
    if prompt_tokens is not None:
        prompt_total = prompt_tokens + (cache_read or 0) + (cache_write or 0)
        token_attrs["llm.token_count.prompt"] = prompt_total
    if completion_tokens is not None:
        token_attrs["llm.token_count.completion"] = completion_tokens
    if cache_read is not None:
        token_attrs["llm.token_count.prompt_details.cache_read"] = cache_read
    if cache_write is not None:
        token_attrs["llm.token_count.prompt_details.cache_write"] = cache_write
    if prompt_total is not None and completion_tokens is not None:
        token_attrs["llm.token_count.total"] = prompt_total + completion_tokens
    if model:
        token_attrs["llm.model_name"] = model

    # Flush deferred LLM span(s) before Session End so strict OTLP backends see
    # parent first. Same at-most-once usage attribution as _handle_stop.
    llm_entries, flush_complete = _flush_deferred_llm_spans(
        gen_id, trace_id, now_ms, {} if usage_attributed else token_attrs
    )
    if not flush_complete:
        return False

    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id
    if user_id:
        attrs["user.id"] = user_id
    if duration_ms is not None:
        attrs["cursor.session.duration_ms"] = duration_ms
    if final_status:
        attrs["cursor.session.final_status"] = final_status
    if reason:
        attrs["cursor.session.reason"] = reason
    if model and not llm_entries:
        attrs["llm.model_name"] = model

    # Fallback (no deferred afterAgentResponse): keep token attrs on Session End.
    if not usage_attributed and not llm_entries:
        attrs.update(token_attrs)
    span = build_span(
        "Session End",
        "CHAIN",
        sid,
        trace_id,
        parent,
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    sent = send_span(span)
    if (
        sent
        and not usage_attributed
        and not llm_entries
        and any(key.startswith("llm.token_count.") for key in token_attrs)
    ):
        generation_terminal_attribution_note_usage(gen_id)
    log(f"sessionEnd: span {sid}, gen={gen_id}")
    return sent


_DEDICATED_TOOL_NAMES = frozenset(
    {
        "shell",
        "terminal",
        "bash",
        "run_command",
        "run_shell",
        "read_file",
        "read",
        "view_file",
        "view",
        "edit_file",
        "edit",
        "write_file",
        "write",
        "create_file",
        "delete_file",
        "tab_file_read",
        "tab_file_edit",
        "mcp",
        "mcp_execution",
    }
)


def _is_mcp_generic_tool_name(tool_name: str) -> bool:
    """Whether a generic tool event names an MCP call (host shape ``MCP:<tool>``)."""
    return tool_name.lower().startswith("mcp:")


def _canonical_tool_input(value) -> str:
    """Canonicalize a tool argument payload so both channels hash alike.

    The generic events carry ``tool_input`` as an object while the dedicated
    MCP events carry the same arguments as a JSON string, so both are parsed
    and re-serialized deterministically before hashing.
    """
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return value
    return _json_string(value)


def _mcp_correlation_digest(mcp_tool_name: str, tool_input) -> str:
    """Digest identifying *what* was called, shared by both event channels.

    The dedicated MCP payloads carry no ``tool_use_id`` (verified against
    Cursor 2026.07.16 raw hook payloads), so correlation cannot use a shared
    id. They do carry the same ``tool_name``/``tool_input`` as the generic
    events, which ties a result to a call by content rather than by arrival
    order. Only this digest is persisted — never raw ids or argument text.
    """
    return stable_digest(f"{mcp_tool_name}\0{_canonical_tool_input(tool_input)}")


def _mcp_tool_name_from_generic(generic_tool_name: str) -> str:
    """Strip the generic ``MCP:`` prefix so both channels name the tool alike."""
    if _is_mcp_generic_tool_name(generic_tool_name):
        return generic_tool_name[len("MCP:") :]
    return generic_tool_name


def _mcp_pair_state_key(gen_id: str) -> str:
    """State key holding the un-merged ``beforeMCPExecution`` records."""
    return f"mcp_{generation_state_key(gen_id)}"


def _mcp_outcome_digest(failed: bool, outcome) -> str:
    """Digest of how a call ended, the field both channels report identically.

    Success and failure are separate domains: a call that returned ``"same"``
    and a *different* call that failed with ``"same"`` are distinguishable in
    the payloads, so they must not share an identity.
    """
    domain = "failure" if failed else "success"
    return stable_digest(f"{domain}\0{_canonical_tool_input(outcome)}")


def _mcp_dedicated_reported_key(gen_id: str, correlation: str, outcome_digest: str) -> str:
    """Key recording that a dedicated MCP span already reported one exact call."""
    return _dedicated_reported_key(gen_id, "mcp", correlation, outcome_digest)


def _dedicated_reported_key(gen_id: str, family: str, correlation: str, outcome_digest: str) -> str:
    """Raw-free marker for a confirmed dedicated span and exact outcome."""
    identity = stable_digest(f"{family}\0{correlation}\0{outcome_digest}")
    return f"dedicateddone_{generation_state_key(gen_id)}_{identity}"


def _shell_command_from_generic(tool_input) -> str:
    if isinstance(tool_input, dict):
        return _jq_str(tool_input, "command", "shell_command")
    if isinstance(tool_input, str):
        try:
            parsed = json.loads(tool_input)
        except (TypeError, ValueError):
            return tool_input
        if isinstance(parsed, dict):
            return _jq_str(parsed, "command", "shell_command")
    return ""


def _generic_completion_claim_key(gen_id: str, invocation_digest: str) -> str:
    """Once-only claim key for one generic invocation, from a validated digest."""
    if len(invocation_digest) != 64 or any(char not in "0123456789abcdef" for char in invocation_digest):
        raise ValueError("invalid invocation digest")
    return f"tooldone_{generation_state_key(gen_id)}_{invocation_digest}"


def _claim_generic_completion_digest(gen_id: str, invocation_digest: str) -> bool:
    """Whether this delivery owns the single span for a digested invocation."""
    if not gen_id or not invocation_digest:
        return True
    return state_claim_once(_generic_completion_claim_key(gen_id, invocation_digest))


def _dedicated_span_already_reported(gen_id: str, tool_name: str, tool_input, failed: bool, outcome) -> bool:
    """Whether a dedicated span already reported *this exact* generic call.

    The decision is deferred to the generic completion so it can be matched on
    the result as well as the call: a dedicated pair that merely looks like an
    open generic invocation is not enough, because a host delivering only half
    the hooks for two different calls produces exactly that shape. Consuming
    the record makes the match one-for-one, so an unmatched completion keeps
    its span rather than being suppressed by a stranger's result.
    """
    if not gen_id:
        return False
    if _is_mcp_generic_tool_name(tool_name):
        correlation = _mcp_correlation_digest(_mcp_tool_name_from_generic(tool_name), tool_input)
        key = _mcp_dedicated_reported_key(gen_id, correlation, _mcp_outcome_digest(failed, outcome))
        return state_pop(key) is not None
    if tool_name.lower() in {"shell", "terminal", "bash", "run_command", "run_shell"}:
        command = _shell_command_from_generic(tool_input)
        if not command or failed:
            # Dedicated shell failures have no dedicated failure event in the
            # current contract; missing command identity stays fail-closed.
            return False
        key = _dedicated_reported_key(gen_id, "shell", stable_digest(command), _mcp_outcome_digest(False, outcome))
        return state_pop(key) is not None
    return False


def _claim_generic_completion(gen_id: str, tool_use_id: str) -> bool:
    """Whether this delivery owns the single span for a generic invocation.

    The claim is keyed by the invocation's own ``tool_use_id``, so a retried
    ``postToolUse`` / ``postToolUseFailure`` for the same id is recognized as
    duplicate delivery instead of emitting twice; success and failure share
    this one identity. Payloads without a generation or invocation id cannot
    be correlated, so they always emit — losing a separate call is worse than
    a rare duplicate.
    """
    if not gen_id or not tool_use_id:
        return True
    return _claim_generic_completion_digest(gen_id, stable_digest(tool_use_id))


def _tool_state_key(gen_id: str, tool_use_id: str) -> str:
    """Parallel-safe key preserving exact authoritative tool-use identity."""
    tool_digest = stable_digest(tool_use_id)
    return f"tool_{generation_state_key(gen_id)}_{tool_digest}"


def _tool_input_with_privacy_provenance(state_key: str, popped: Optional[dict]) -> Tuple[str, bool]:
    """Resolve tool input while retaining content-free duplicate-delivery provenance."""
    privacy_key = f"{state_key}_privacy"
    privacy_state = state_pop(privacy_key)
    privacy_record = dict(privacy_state or {})
    if popped:
        source = popped.get("tool_input", "")
        was_redacted = bool(popped.get("tool_input_redacted", False))
    elif privacy_state:
        source = privacy_state.get("tool_input", "")
        was_redacted = bool(privacy_state.get("tool_input_redacted", False))
    else:
        return "", False

    tool_input = _redact_deferred(env.log_tool_content, source, was_redacted)
    is_redacted = was_redacted or not env.log_tool_content
    privacy_record["tool_input"] = tool_input if is_redacted else ""
    privacy_record["tool_input_redacted"] = is_redacted
    state_push(privacy_key, privacy_record)
    return tool_input, is_redacted


def _handle_pre_tool_use(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Capture generic tool input before execution, keyed by tool_use_id."""
    if not gen_id:
        return
    tool_use_id = _jq_str(input_json, "tool_use_id")
    if not tool_use_id:
        return
    tool_content_allowed = env.log_tool_content
    tool_name = _jq_str(input_json, "tool_name")
    state_key = _tool_state_key(gen_id, tool_use_id)
    while state_pop(f"{state_key}_privacy"):
        pass
    state_push(
        state_key,
        {
            "start_ms": now_ms,
            "tool_name": tool_name,
            "tool_input": redact_content(tool_content_allowed, _json_string(input_json.get("tool_input"))),
            "tool_input_redacted": not tool_content_allowed,
        },
    )
    log(f"preToolUse: pushed state for tool_use_id={tool_use_id}")


def _handle_post_tool_use(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for a successful generic tool call, retrying failed sends."""
    tool_name = _jq_str(input_json, "tool_name", "toolName", "name", "tool")
    tool_use_id = _jq_str(input_json, "tool_use_id")
    state_key = _tool_state_key(gen_id, tool_use_id) if gen_id and tool_use_id else ""

    def deliver() -> bool:
        popped_entries = state_peek_all(state_key) if state_key else []
        popped = popped_entries[-1] if popped_entries else None
        # Legacy completion-only dedicated events have no correlatable generic
        # invocation. Keep suppressing those, while a real preToolUse record is
        # allowed to provide fallback telemetry when no rich span was delivered.
        if tool_name.lower() in _DEDICATED_TOOL_NAMES and popped is None:
            log(f"postToolUse: skipping {tool_name!r} — uncorrelatable dedicated completion")
            return True

        raw_output = input_json.get("tool_output")
        if raw_output is None:
            raw_output = _jq_str(input_json, "result", "output", "response", "stdout")
        if _dedicated_span_already_reported(gen_id, tool_name, input_json.get("tool_input"), False, raw_output):
            if state_key:
                state_pop(state_key)
            log(f"postToolUse: skipping {tool_name!r} — exact call already reported by dedicated span")
            return True

        sid = span_id_16()
        parent = gen_root_span_get(gen_id) if gen_id else ""
        tool_input, input_is_redacted = (
            _tool_input_with_privacy_provenance(state_key, popped) if state_key else ("", False)
        )
        if not tool_input and not input_is_redacted:
            raw_input = input_json.get("tool_input")
            if raw_input is None:
                raw_input = _jq_str(input_json, "toolInput", "input", "arguments", "args")
            serialized_input = _json_string(raw_input)
            tool_input = (
                _redact_terminal_field(f"{state_key}_privacy", "tool_input", serialized_input, env.log_tool_content)[0]
                if state_key
                else redact_content(env.log_tool_content, serialized_input)
            )
        serialized_output = _json_string(raw_output)
        output = (
            _redact_terminal_field(f"{state_key}_privacy", "tool_output", serialized_output, env.log_tool_content)[0]
            if state_key
            else redact_content(env.log_tool_content, serialized_output)
        )
        duration = _to_int(input_json.get("duration"))
        start_ms = popped.get("start_ms", now_ms) if popped else now_ms - max(duration or 0, 0)

        attrs = {"openinference.span.kind": "TOOL", "session.id": conversation_id}
        if conversation_id:
            attrs["cursor.conversation.id"] = conversation_id
        user_id = _resolve_user_id(input_json)
        if user_id:
            attrs["user.id"] = user_id
        if tool_name:
            attrs["tool.name"] = tool_name
        if tool_input:
            attrs["input.value"] = tool_input
        if output:
            attrs["output.value"] = output

        span = build_span(
            f"Tool: {tool_name}" if tool_name else "Tool Use",
            "TOOL",
            sid,
            trace_id,
            parent,
            start_ms,
            now_ms,
            attrs,
            SERVICE_NAME,
            SCOPE_NAME,
        )
        sent = send_span(span)
        if sent and state_key:
            state_pop(state_key)
        if sent:
            log(f"postToolUse: span {sid} (tool={tool_name})")
        return sent

    if gen_id and tool_use_id:
        state_complete_once(_generic_completion_claim_key(gen_id, stable_digest(tool_use_id)), deliver)
    else:
        deliver()


def _handle_post_tool_use_failure(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Emit a retryable TOOL span for failure, timeout, denial, or interruption."""
    tool_name = _jq_str(input_json, "tool_name") or "unknown"
    tool_use_id = _jq_str(input_json, "tool_use_id")
    state_key = _tool_state_key(gen_id, tool_use_id) if gen_id and tool_use_id else ""

    def deliver() -> bool:
        popped_entries = state_peek_all(state_key) if state_key else []
        popped = popped_entries[-1] if popped_entries else None
        tool_input, input_is_redacted = (
            _tool_input_with_privacy_provenance(state_key, popped) if state_key else ("", False)
        )
        if not tool_input and not input_is_redacted:
            serialized_input = _json_string(input_json.get("tool_input"))
            tool_input = (
                _redact_terminal_field(f"{state_key}_privacy", "tool_input", serialized_input, env.log_tool_content)[0]
                if state_key
                else redact_content(env.log_tool_content, serialized_input)
            )
        raw_error = _jq_str(input_json, "error_message")
        error_message = (
            _redact_terminal_field(f"{state_key}_privacy", "error_message", raw_error, env.log_tool_content)[0]
            if state_key
            else redact_content(env.log_tool_content, raw_error)
        )
        failure_type = _jq_str(input_json, "failure_type")
        duration = _to_int(input_json.get("duration"))
        start_ms = popped.get("start_ms", now_ms) if popped else now_ms - max(duration or 0, 0)

        if _dedicated_span_already_reported(gen_id, tool_name, input_json.get("tool_input"), True, raw_error):
            if state_key:
                state_pop(state_key)
            log(f"postToolUseFailure: skipping {tool_name!r} — exact call already reported by dedicated span")
            return True

        attrs = {
            "openinference.span.kind": "TOOL",
            "tool.name": tool_name,
            "input.value": tool_input,
            "output.value": error_message,
            "session.id": conversation_id,
            "cursor.tool.status": "error",
            "cursor.tool.failure_type": failure_type,
            "cursor.tool.is_interrupt": bool(input_json.get("is_interrupt", False)),
        }
        if conversation_id:
            attrs["cursor.conversation.id"] = conversation_id
        user_id = _resolve_user_id(input_json)
        if user_id:
            attrs["user.id"] = user_id

        span = build_span(
            f"Tool: {tool_name}",
            "TOOL",
            span_id_16(),
            trace_id,
            gen_root_span_get(gen_id) if gen_id else "",
            start_ms,
            now_ms,
            attrs,
            SERVICE_NAME,
            SCOPE_NAME,
            status_code=2,
            status_message=truncate_attr(error_message, 256),
        )
        sent = send_span(span)
        if sent and state_key:
            state_pop(state_key)
        if sent:
            log(f"postToolUseFailure: span for tool={tool_name}")
        return sent

    if gen_id and tool_use_id:
        state_complete_once(_generic_completion_claim_key(gen_id, stable_digest(tool_use_id)), deliver)
    else:
        deliver()


def _subagent_state_key(gen_id: str, subagent_type: str, task: str) -> str:
    """Key subagent state from fields declared on both lifecycle events.

    Cursor's declared ``subagentStop`` schema omits ``subagent_id``.  Hash the
    shared type/task tuple so task content is not exposed in a state filename.
    Identical concurrent tasks share a LIFO stack rather than claiming ID-safe
    pairing the host contract cannot provide.
    """
    correlation = stable_digest(f"{subagent_type}\0{task}")[:24]
    return f"subagent_{generation_state_key(gen_id)}_{correlation}"


def _handle_subagent_start(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Capture subagent start state using declared cross-event fields."""
    if not gen_id:
        return
    subagent_id = _jq_str(input_json, "subagent_id")
    subagent_type = _jq_str(input_json, "subagent_type")
    raw_task = _jq_str(input_json, "task")
    state_key = _subagent_state_key(gen_id, subagent_type, raw_task)
    while state_pop(f"{state_key}_privacy"):
        pass
    state_push(
        state_key,
        {
            "start_ms": now_ms,
            "task": redact_content(env.log_prompts, raw_task),
            "task_redacted": not env.log_prompts,
            "subagent_type": subagent_type,
            "subagent_id": subagent_id,
        },
    )
    log("subagentStart: pushed contract-correlated state")


def _handle_subagent_stop(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Emit one CHAIN span for a completed, failed, or aborted subagent."""
    raw_task = _jq_str(input_json, "task")
    stop_type = _jq_str(input_json, "subagent_type")
    state_key = _subagent_state_key(gen_id, stop_type, raw_task) if gen_id else ""
    popped = state_pop(state_key) if state_key else None
    privacy_key = f"{state_key}_privacy" if state_key else ""
    privacy_state = state_pop(privacy_key) if privacy_key else None
    subagent_id = (popped or {}).get("subagent_id", "") or _jq_str(input_json, "subagent_id")
    duration = _to_int(input_json.get("duration_ms"))
    start_ms = popped.get("start_ms", now_ms) if popped else now_ms - max(duration or 0, 0)
    subagent_type = stop_type or (popped or {}).get("subagent_type", "")
    if popped:
        task_source = popped.get("task", "")
        task_was_redacted = bool(popped.get("task_redacted", False))
    elif privacy_state:
        task_source = privacy_state.get("task", "")
        task_was_redacted = bool(privacy_state.get("task_redacted", False))
    else:
        task_source = raw_task
        task_was_redacted = False
    task = _redact_deferred(env.log_prompts, task_source, task_was_redacted)
    task_is_redacted = task_was_redacted or not env.log_prompts
    if privacy_key:
        privacy_record = dict(privacy_state or {})
        privacy_record["task"] = task if task_is_redacted else ""
        privacy_record["task_redacted"] = task_is_redacted
        state_push(privacy_key, privacy_record)
    raw_summary = _jq_str(input_json, "summary", "error_message")
    summary = (
        _redact_terminal_field(privacy_key, "summary", raw_summary, env.log_model_outputs)[0]
        if privacy_key
        else redact_content(env.log_model_outputs, raw_summary)
    )
    attrs = {
        "openinference.span.kind": "CHAIN",
        "input.value": task,
        "output.value": summary,
        "session.id": conversation_id,
        "cursor.subagent.id": subagent_id,
        "cursor.subagent.type": subagent_type,
        "cursor.subagent.status": _jq_str(input_json, "status"),
    }
    if conversation_id:
        attrs["cursor.conversation.id"] = conversation_id

    span = build_span(
        f"Subagent: {subagent_type or 'unknown'}",
        "CHAIN",
        span_id_16(),
        trace_id,
        gen_root_span_get(gen_id) if gen_id else "",
        start_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)
    log(f"subagentStop: span for subagent_id={subagent_id}")


def _handle_pre_compact(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Record context compaction without capturing transcript content."""
    attrs = {
        "openinference.span.kind": "CHAIN",
        "session.id": conversation_id,
        "cursor.compact.trigger": _jq_str(input_json, "trigger"),
    }
    for field in ("context_usage_percent", "context_tokens", "context_window_size", "message_count"):
        value = _to_int(input_json.get(field))
        if value is not None:
            attrs[f"cursor.compact.{field}"] = value
    span = build_span(
        "Context Compaction",
        "CHAIN",
        span_id_16(),
        trace_id,
        gen_root_span_get(gen_id) if gen_id else "",
        now_ms,
        now_ms,
        attrs,
        SERVICE_NAME,
        SCOPE_NAME,
    )
    send_span(span)


def _handle_workspace_open(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Do not create a trace for an app lifecycle event without a session."""
    log("workspaceOpen: tracing hook loaded")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    """Entry point for arize-hook-cursor. Cursor hook.

    Input contract: JSON on stdin, all registered events routed here.
    stdout: MUST print permissive JSON response, even on error.
    stderr: redirected to ARIZE_LOG_FILE at adapter import time via
        core.common.redirect_stderr_to_log_file().
    """
    event = ""
    try:
        if not _sweep_pending_generation_cleanups():
            return
        if not check_requirements():
            return

        input_json = json.loads(sys.stdin.read() or "{}")
        event = _event_name(input_json)
        _dispatch(event, input_json, sweep_pending=False)
    except Exception as e:
        error(f"cursor hook failed ({event}): {e}")
    finally:
        # ALWAYS print permissive response — this is the LAST thing that happens
        _print_permissive(event)
