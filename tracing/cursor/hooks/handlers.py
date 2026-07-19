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
    generation_completion_status,
    generation_digest_completion_status,
    generation_finish_pending_cleanup_batch,
    generation_guard,
    generation_mark_cleanup_done,
    generation_mark_completed,
    generation_mark_digest_completed,
    generation_pending_cleanup_batch,
    generation_state_key,
    span_id_16,
    stable_digest,
    state_cleanup_generation,
    state_cleanup_generation_digest,
    state_pop,
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
                fallback_digest = stable_digest(f"cursor-terminal-fallback\0{conversation_id}")
                completion_status = generation_digest_completion_status(fallback_digest)
                if completion_status != "active":
                    log(f"Ignoring {event} without generation: {completion_status}")
                    return
                # Claim before terminal telemetry effects. No generation-scoped
                # cleanup is possible because this degraded payload has no generation.
                generation_mark_digest_completed(fallback_digest)
            else:
                completion_status = generation_completion_status("")
                if completion_status != "active":
                    log(f"Ignoring {event} without generation: {completion_status}")
                    return
            handler(input_json, conversation_id, gen_id, trace_id, now_ms)
        return

    # A bounded striped guard makes completion-check → handler atomic with
    # stop's mark → cleanup sequence. The global ledger guard additionally
    # orders every handler against saturation transitions across generations.
    with generation_guard(gen_id):
        with completion_ledger_guard():
            completion_status = generation_completion_status(gen_id)
            if completion_status != "active":
                log(f"Ignoring {event} for generation: {completion_status}")
                return
            if event in {"stop", "sessionEnd"}:
                # Claim at-most-once terminal delivery and durable cleanup work
                # in one ledger transaction before any handler side effect.
                generation_mark_completed(gen_id, cleanup_pending=True)
                try:
                    handler(input_json, conversation_id, gen_id, trace_id, now_ms)
                finally:
                    state_cleanup_generation(gen_id)
                    generation_mark_cleanup_done(stable_digest(gen_id))
            else:
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

    privacy_key = f"shell_privacy_{generation_state_key(gen_id)}"
    while state_pop(privacy_key):
        pass
    state_push(
        f"shell_{generation_state_key(gen_id)}",
        {
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
    privacy_key = f"shell_privacy_{generation_state_key(gen_id)}" if gen_id else ""
    popped = state_pop(f"shell_{generation_state_key(gen_id)}") if gen_id else None
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
    after_cmd = _jq_str(input_json, "command", "shell_command")
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
    send_span(span)
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
    state_key = f"mcp_{generation_state_key(gen_id)}"
    while state_pop(f"{state_key}_privacy"):
        pass

    state_push(
        state_key,
        {
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
    state_key = f"mcp_{generation_state_key(gen_id)}" if gen_id else ""
    popped = state_pop(state_key) if state_key else None

    if popped:
        start_ms = popped.get("start_ms", "")
        tool_name = popped.get("tool_name", "")
    else:
        start_ms = ""
        tool_name = ""
    tool_input, _ = _tool_input_with_privacy_provenance(state_key, popped) if state_key else ("", False)
    start_ms = start_ms or str(now_ms)

    # Override tool name from after-event if present
    after_tool = _jq_str(input_json, "tool_name", "toolName", "name")
    if after_tool:
        tool_name = after_tool
    tool_name = tool_name or "unknown"

    raw_result = _jq_str(input_json, "result", "output", "result_json")
    result = (
        _redact_terminal_field(f"{state_key}_privacy", "result", raw_result, env.log_tool_content)[0]
        if state_key
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
    )
    send_span(span)
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


def _handle_stop(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Flush deferred LLM span(s) with per-turn tokens, then send Agent Stop CHAIN + cleanup."""
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    status = _jq_str(input_json, "status", "reason")
    loop_count = _jq_str(input_json, "loop_count", "loopCount", "iterations")

    user_id = _resolve_user_id(input_json)

    # Token counts from stop payload
    # Use explicit None checks — 0 is a valid token count but falsy with ``or``
    _inp_tok = input_json.get("input_tokens")
    prompt_tokens = _to_int(_inp_tok if _inp_tok is not None else input_json.get("inputTokens"))
    _out_tok = input_json.get("output_tokens")
    completion_tokens = _to_int(_out_tok if _out_tok is not None else input_json.get("outputTokens"))
    _cr_tok = input_json.get("cache_read_tokens")
    cache_read = _to_int(_cr_tok if _cr_tok is not None else input_json.get("cacheReadTokens"))
    _cw_tok = input_json.get("cache_write_tokens")
    cache_write = _to_int(_cw_tok if _cw_tok is not None else input_json.get("cacheWriteTokens"))
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

    # Drain deferred LLM stack for this generation (LIFO: first pop = most recent).
    llm_entries = []
    if gen_id:
        llm_key = f"llm_{generation_state_key(gen_id)}"
        while True:
            entry = state_pop(llm_key)
            if entry is None:
                break
            llm_entries.append(entry)

    # Flush deferred LLM span(s) before Agent Stop so strict OTLP backends see parent first.
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
        send_span(llm_span)

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
    if not llm_entries:
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
    send_span(span)
    log(f"stop: span {sid}, gen={gen_id}")


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
    """CHAIN span for Cursor CLI sessionEnd event — closes the session.

    Reuses tokens/duration from the payload when present.  Cleanup is owned by
    the dispatcher's terminal claim/finally lifecycle.
    """
    sid = span_id_16()
    parent = gen_root_span_get(gen_id)

    _dur = input_json.get("duration_ms")
    duration_ms = _to_int(_dur if _dur is not None else input_json.get("durationMs"))
    final_status = _jq_str(input_json, "final_status", "finalStatus", "status")
    reason = _jq_str(input_json, "reason")

    user_id = _resolve_user_id(input_json)

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

    # Token fields can also appear on sessionEnd. Same OpenInference convention
    # as _handle_stop: ``prompt`` is the total (uncached input + cache buckets),
    # cache split reported via ``prompt_details.*`` subsets.
    _inp_tok = input_json.get("input_tokens")
    prompt_tokens = _to_int(_inp_tok if _inp_tok is not None else input_json.get("inputTokens"))
    _out_tok = input_json.get("output_tokens")
    completion_tokens = _to_int(_out_tok if _out_tok is not None else input_json.get("outputTokens"))
    _cr_tok = input_json.get("cache_read_tokens")
    cache_read = _to_int(_cr_tok if _cr_tok is not None else input_json.get("cacheReadTokens"))
    _cw_tok = input_json.get("cache_write_tokens")
    cache_write = _to_int(_cw_tok if _cw_tok is not None else input_json.get("cacheWriteTokens"))
    prompt_total = None
    if prompt_tokens is not None:
        prompt_total = prompt_tokens + (cache_read or 0) + (cache_write or 0)
        attrs["llm.token_count.prompt"] = prompt_total
    if completion_tokens is not None:
        attrs["llm.token_count.completion"] = completion_tokens
    if cache_read is not None:
        attrs["llm.token_count.prompt_details.cache_read"] = cache_read
    if cache_write is not None:
        attrs["llm.token_count.prompt_details.cache_write"] = cache_write
    if prompt_total is not None and completion_tokens is not None:
        attrs["llm.token_count.total"] = prompt_total + completion_tokens

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
    send_span(span)
    log(f"sessionEnd: span {sid}, gen={gen_id}")


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
    tool_use_id = _jq_str(input_json, "tool_use_id")
    if not tool_use_id:
        return
    tool_content_allowed = env.log_tool_content
    state_key = _tool_state_key(gen_id, tool_use_id)
    while state_pop(f"{state_key}_privacy"):
        pass
    state_push(
        state_key,
        {
            "start_ms": now_ms,
            "tool_name": _jq_str(input_json, "tool_name"),
            "tool_input": redact_content(tool_content_allowed, _json_string(input_json.get("tool_input"))),
            "tool_input_redacted": not tool_content_allowed,
        },
    )
    log(f"preToolUse: pushed state for tool_use_id={tool_use_id}")


def _handle_post_tool_use(input_json, conversation_id, gen_id, trace_id, now_ms):
    """TOOL span for a successful generic tool call."""
    tool_name = _jq_str(input_json, "tool_name", "toolName", "name", "tool")
    tool_use_id = _jq_str(input_json, "tool_use_id")
    state_key = _tool_state_key(gen_id, tool_use_id) if tool_use_id else ""
    popped = state_pop(state_key) if state_key else None

    # Dedicated hooks provide richer fields. Still pop generic state so a host
    # emitting both APIs does not leave dangling files.
    if tool_name.lower() in _DEDICATED_TOOL_NAMES:
        log(f"postToolUse: skipping {tool_name!r} — covered by dedicated handler")
        return

    sid = span_id_16()
    parent = gen_root_span_get(gen_id) if gen_id else ""
    tool_input, input_is_redacted = _tool_input_with_privacy_provenance(state_key, popped) if state_key else ("", False)
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
    raw_output = input_json.get("tool_output")
    if raw_output is None:
        raw_output = _jq_str(input_json, "result", "output", "response", "stdout")
    serialized_output = _json_string(raw_output)
    output = (
        _redact_terminal_field(f"{state_key}_privacy", "tool_output", serialized_output, env.log_tool_content)[0]
        if state_key
        else redact_content(env.log_tool_content, serialized_output)
    )
    duration = _to_int(input_json.get("duration"))
    start_ms = popped.get("start_ms", now_ms) if popped else now_ms - max(duration or 0, 0)

    attrs = {
        "openinference.span.kind": "TOOL",
        "session.id": conversation_id,
    }
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
    send_span(span)
    log(f"postToolUse: span {sid} (tool={tool_name})")


def _handle_post_tool_use_failure(input_json, conversation_id, gen_id, trace_id, now_ms):
    """Emit a TOOL span for failures, timeouts, denial, or interruption."""
    tool_name = _jq_str(input_json, "tool_name") or "unknown"
    tool_use_id = _jq_str(input_json, "tool_use_id")
    state_key = _tool_state_key(gen_id, tool_use_id) if tool_use_id else ""
    popped = state_pop(state_key) if state_key else None
    tool_input, input_is_redacted = _tool_input_with_privacy_provenance(state_key, popped) if state_key else ("", False)
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
    )
    send_span(span)
    log(f"postToolUseFailure: span for tool={tool_name}")


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
    state_key = _subagent_state_key(gen_id, stop_type, raw_task)
    popped = state_pop(state_key)
    privacy_key = f"{state_key}_privacy"
    privacy_state = state_pop(privacy_key)
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
    privacy_record = dict(privacy_state or {})
    privacy_record["task"] = task if task_is_redacted else ""
    privacy_record["task_redacted"] = task_is_redacted
    state_push(privacy_key, privacy_record)
    raw_summary = _jq_str(input_json, "summary", "error_message")
    summary = _redact_terminal_field(privacy_key, "summary", raw_summary, env.log_model_outputs)[0]
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
