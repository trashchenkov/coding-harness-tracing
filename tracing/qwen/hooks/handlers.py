"""Qwen Code hook handlers. One exported function per hook event.

Each function is a CLI entry point registered in pyproject.toml
[project.scripts]. Hooks must never fail the host process, so every entry point
swallows its exceptions after logging them.

The turn, not the session, is the unit of tracing — the same choice
``tracing/claude_code`` makes. ``UserPromptSubmit`` opens a turn and ``Stop``
exports it; ``SessionEnd`` only cleans up. This matters because Qwen Code does
not emit ``SessionEnd`` at all in one-shot (``qwen "prompt"``) mode, so a
session-scoped root span would never close there.
"""

import hashlib
import json
import os
import stat
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from core.common import (
    FileLock,
    env,
    error,
    generate_span_id,
    generate_trace_id,
    get_timestamp_ms,
    log,
    redact_content,
    redirect_stderr_to_log_file,
    send_span,
)
from core.constants import HARNESSES
from core.event_model import AgentEvent, EventStatus, ToolEvent, TurnEvent
from core.span_renderer import render_event_graph
from tracing.qwen.constants import TODO_PHASE_POSTWRITE

from .adapter import (
    SCOPE_NAME,
    SERVICE_NAME,
    ensure_session_initialized,
    gc_stale_state_files,
    resolve_agent_transcript_path,
    resolve_session,
    resolve_transcript_path,
)
from .transcript import parse_qwen_transcript

TRANSCRIPT_STABILITY_RETRIES = 50
TRANSCRIPT_STABILITY_DELAY_SECONDS = 0.02


class _InitializingOperationLock:
    def __init__(self, state, path: Path) -> None:
        self._state = state
        self._lock = FileLock(path, timeout=10.0, break_on_timeout=False)
        self.lock_path = self._lock.lock_path

    def __enter__(self):
        self._lock.__enter__()
        try:
            self._state.init_state()
        except Exception:
            self._lock.__exit__(*sys.exc_info())
            raise
        self._state._active_qwen_operation_lock = self
        return self

    @contextmanager
    def suspended(self):
        """Release a shard while transport blocks, then reacquire it."""
        self._lock.__exit__(None, None, None)
        try:
            yield
        finally:
            self._lock.__enter__()
            self._state.init_state()

    def __exit__(self, *args) -> None:
        self._state._active_qwen_operation_lock = None
        self._lock.__exit__(*args)


def _operation_lock(state, name: str) -> _InitializingOperationLock:
    """Serialize lifecycle work and initialize state only while locked."""
    base = state._lock_path
    if base is None:
        if state.state_file is None:
            raise RuntimeError("Qwen state has no lock path")
        base = state.state_file.with_suffix(".lock")
    path = base.with_name(f"{base.name}.{name}")
    return _InitializingOperationLock(state, path)


def _transcript_snapshot(transcript: Path, start_line: int) -> Optional[str]:
    """Read one stable regular JSONL file descriptor without following links."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(transcript, flags)
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        proc_fd = Path(f"/proc/self/fd/{fd}")
        if proc_fd.exists():
            try:
                if proc_fd.resolve(strict=True) != transcript.absolute():
                    return None
            except (OSError, RuntimeError):
                return None
        chunks = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        payload = b"".join(chunks)
        after = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)

    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or after.st_size != len(payload)
        or (payload and not payload.endswith(b"\n"))
    ):
        return None
    try:
        text = payload.decode("utf-8")
    except UnicodeError:
        return None
    lines = text.splitlines()
    tail = [line for line in lines[max(0, start_line) :] if line.strip()]
    for line in tail:
        try:
            json.loads(line)
        except (json.JSONDecodeError, TypeError):
            return None
    return text


def _count_complete_transcript_lines(transcript: Path) -> Optional[int]:
    """Count newlines from one stable regular descriptor without following links."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(transcript, flags)
    except OSError:
        return None
    try:
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return None
        proc_fd = Path(f"/proc/self/fd/{fd}")
        if proc_fd.exists():
            try:
                if proc_fd.resolve(strict=True) != transcript.absolute():
                    return None
            except (OSError, RuntimeError):
                return None
        line_count = 0
        bytes_read = 0
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            bytes_read += len(chunk)
            line_count += chunk.count(b"\n")
        after = os.fstat(fd)
    except OSError:
        return None
    finally:
        os.close(fd)
    if (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or before.st_ctime_ns != after.st_ctime_ns
        or after.st_size != bytes_read
    ):
        return None
    return line_count


def _tail_has_assistant_message(
    snapshot: str,
    start_line: int,
    expected: str,
    expected_sha256: str = "",
    expected_session_id: str = "",
    allow_latest_assistant: bool = False,
) -> bool:
    """Return whether the completed tail contains Qwen's final assistant record."""
    assistant_texts = []
    for raw_line in snapshot.splitlines()[max(0, start_line) :]:
        if not raw_line.strip():
            continue
        entry = json.loads(raw_line)
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        if expected_session_id and entry.get("sessionId") != expected_session_id:
            continue
        message = entry.get("message")
        parts = message.get("parts") if isinstance(message, dict) else None
        if not isinstance(parts, list):
            continue
        assistant_texts.append(
            "".join(
                part.get("text", "")
                for part in parts
                if isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought")
            )
        )
    if not assistant_texts:
        return False
    if allow_latest_assistant:
        return True
    if expected_sha256:
        actual = hashlib.sha256(assistant_texts[-1].encode("utf-8")).hexdigest()
        return actual == expected_sha256
    if expected:
        return assistant_texts[-1] == expected
    return allow_latest_assistant


def _wait_for_stable_transcript(
    transcript,
    start_line: int,
    expected_assistant_message: str = "",
    expected_assistant_sha256: str = "",
    expected_session_id: str = "",
    allow_latest_assistant: bool = False,
) -> Optional[str]:
    """Wait until Qwen's final assistant record is present; return that snapshot."""
    for attempt in range(TRANSCRIPT_STABILITY_RETRIES + 1):
        snapshot = _transcript_snapshot(transcript, start_line)
        if snapshot is not None and _tail_has_assistant_message(
            snapshot,
            start_line,
            expected_assistant_message,
            expected_assistant_sha256,
            expected_session_id,
            allow_latest_assistant,
        ):
            return snapshot
        if attempt < TRANSCRIPT_STABILITY_RETRIES:
            time.sleep(TRANSCRIPT_STABILITY_DELAY_SECONDS)
    return None


def _read_stdin() -> dict:
    """Read hook input JSON from stdin, tolerating empty or malformed payloads.

    The payload is decoded from bytes as UTF-8 rather than read as text. Hook
    payloads carry user prompts and tool output, and ``sys.stdin.read()``
    decodes with the interpreter's locale encoding — on a Windows console set to
    a non-UTF-8 codepage that raises ``UnicodeDecodeError`` on any non-ASCII
    prompt, the entry point swallows it, and the turn silently produces no span.
    Reading bytes removes the locale from the path entirely. See #88, which
    reports the same failure mode for transcript reads.
    """
    try:
        buffer = getattr(sys.stdin, "buffer", None)
        if buffer is not None:
            raw = buffer.read().decode("utf-8", errors="replace")
        else:  # pragma: no cover - stdin replaced by a text-only stub
            raw = sys.stdin.read()
    except (OSError, UnicodeError, ValueError):
        return {}
    if not raw.strip():
        return {}
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return payload if isinstance(payload, dict) else {}


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def _handle_session_start(input_json: dict) -> None:
    """Initialize session state. SessionStart also carries the resolved model."""
    state = resolve_session(input_json)
    with _operation_lock(state, "lifecycle"):
        # A new authoritative SessionStart supersedes the bounded terminal
        # tombstone left by a prior lifecycle using the same key.
        if state.get("session_closed") == "1" and state.state_file is not None:
            state.state_file.unlink(missing_ok=True)
        ensure_session_initialized(state, input_json)
        model = input_json.get("model") or ""
        if model:
            state.set("model", model)
        source = input_json.get("source") or ""
        if source:
            state.set("session_source", source)
    log(f"Session started: {state.get('session_id')} (source={source or 'unknown'})")


def _delivery_lock_path(state) -> Path:
    if state.state_file is None:
        raise RuntimeError("Qwen state has no delivery lock path")
    # Unlike lifecycle locks, delivery locks cannot be sharded: independent
    # sessions must be able to export concurrently even when their shard hashes
    # collide. The state filename already includes the full session namespace.
    return state.state_file.with_name(f"{state.state_file.name}.delivery.lock")


def _delivery_lease_is_held(state) -> bool:
    """Use an OS lock, not a reusable PID, as proof of a live sender."""
    lock = FileLock(_delivery_lock_path(state), timeout=0.0, break_on_timeout=False)
    try:
        lock.__enter__()
    except TimeoutError:
        return True
    else:
        lock.__exit__(None, None, None)
        return False


def _wait_for_live_delivery_lease(state) -> None:
    """Wait through each live sender lease without holding the shared shard."""
    delivery = state.get("export_delivery_state") or ""
    if not delivery.startswith("attempt:"):
        return
    operation_lock = getattr(state, "_active_qwen_operation_lock", None)
    if operation_lock is None:
        return
    deadline = time.monotonic() + 12.0
    with operation_lock.suspended():
        while True:
            current = state.get("export_delivery_state") or ""
            if not current.startswith("attempt:"):
                return
            if current != delivery:
                delivery = current
                deadline = time.monotonic() + 12.0
            if not _delivery_lease_is_held(state):
                return
            if time.monotonic() >= deadline:
                return
            time.sleep(0.01)


def _handle_session_end(input_json: dict) -> None:
    """Close out a session. Exports any turn Stop did not close, then cleans up."""
    state = resolve_session(input_json)
    with _operation_lock(state, "lifecycle"):
        _wait_for_live_delivery_lease(state)
        if state.get("current_trace_id"):
            fallback = dict(input_json)
            fallback.setdefault("last_assistant_message", "(Turn closed by fail-safe: Stop hook did not fire)")
            _handle_stop_locked(state, fallback)
            if state.get("turn_exported") == "1":
                _clear_turn(state)
        trace_count = state.get("trace_count") or "0"
        tool_count = state.get("tool_count") or "0"
        error(f"Session complete: {trace_count} traces, {tool_count} tools")
        error(f"View in Arize/Phoenix: session.id = {state.get('session_id')}")

        if state.get("current_trace_id"):
            log("SessionEnd: final turn export failed; retaining state for retry")
            return

        if state.state_file is not None:
            state.state_file.unlink(missing_ok=True)
            # Keep a minimal, content-free terminal tombstone so an event that
            # resolved this session before SessionEnd cannot recreate it after
            # waiting for the lifecycle lock. Explicit-state TTL GC bounds it.
            state.set("session_closed", "1")
    # Advisory lock files are intentionally persistent. Unlinking one can split
    # concurrent lockers across old/new inodes and destroy mutual exclusion.
    gc_stale_state_files()


# ---------------------------------------------------------------------------
# Turn lifecycle
# ---------------------------------------------------------------------------


def _handle_user_prompt_submit(input_json: dict) -> None:
    """Open a genuine user turn and ignore machine continuation projections.

    ``submitted_prompt`` is Qwen's provenance marker for an interactive user
    submission. Headless mode may omit it, so a non-empty model-bound ``prompt``
    may open a turn only when no turn is already active.
    """
    model_prompt = input_json.get("prompt", "") or ""
    submitted_prompt = input_json.get("submitted_prompt", "") or ""
    prompt = submitted_prompt if submitted_prompt.strip() else model_prompt
    if not prompt.strip():
        return

    state = resolve_session(input_json)

    with _operation_lock(state, "lifecycle"):
        if state.get("session_closed") == "1":
            log("UserPromptSubmit: session already closed; ignoring delayed event")
            return
        ensure_session_initialized(state, input_json)
        prev_trace_id = state.get("current_trace_id") or ""
        if prev_trace_id and not submitted_prompt.strip():
            # Qwen uses the same event for machine-driven continuation projections.
            return

        # Finalize the previous turn before opening a genuine new user turn.
        # A previously exported Stop is definitive now; an unexported turn gets
        # one fail-safe retry using its persisted authoritative digest.
        if prev_trace_id:
            if state.get("turn_exported") == "1":
                _clear_turn(state)
            else:
                retry_input = dict(input_json)
                retry_input.setdefault("last_assistant_message", "(Turn closed by fail-safe: Stop hook did not fire)")
                _handle_stop_locked(state, retry_input)
                if state.get("turn_exported") == "1":
                    _clear_turn(state)
                current = state.get("current_trace_id") or ""
                if current == prev_trace_id:
                    log("Fail-safe: retained orphaned turn after failed export")
                    return
                if current:
                    log("Fail-safe: a concurrent newer turn owns the session state")
                    return

        # Qwen writes the transcript only once it records the first entry, which
        # is after this event. Requiring the file here rejects the canonical path
        # on every opening turn — and in one-shot mode that is the only turn, so
        # the session would produce no spans at all. Confinement is still
        # enforced; only the existence check is deferred to Stop.
        transcript = resolve_transcript_path(input_json, must_exist=False)
        if input_json.get("transcript_path") and transcript is None:
            log("UserPromptSubmit: authoritative transcript path is invalid; turn not opened")
            return
        line_count = (
            _count_complete_transcript_lines(transcript) if transcript is not None and transcript.is_file() else 0
        )
        if line_count is None:
            log("UserPromptSubmit: transcript boundary is unknown; turn not opened")
            return
        trace_count = int(state.get("trace_count") or "0") + 1
        # Apply privacy policy at capture time so retries/crashes never persist
        # content that was opted out when the event arrived.
        privacy = {
            "prompts": env.log_prompts,
            "tool_details": env.log_tool_details,
            "tool_content": env.log_tool_content,
        }
        if (
            state.set_many(
                {
                    "trace_count": str(trace_count),
                    "current_trace_id": generate_trace_id(),
                    "current_trace_span_id": generate_span_id(),
                    "current_trace_start_time": str(get_timestamp_ms()),
                    "current_trace_privacy": json.dumps(privacy, sort_keys=True),
                    "current_trace_prompt": redact_content(privacy["prompts"], prompt),
                    "pending_subagents": "{}",
                    "turn_revision": "0",
                    "trace_start_line": str(line_count),
                }
            )
            is False
        ):
            log("UserPromptSubmit: failed to persist complete turn atomically")


def _turn_privacy(state) -> dict:
    """Return the immutable privacy policy captured when this turn opened."""
    stored = state.get("current_trace_privacy") or ""
    try:
        decoded = json.loads(stored)
    except (json.JSONDecodeError, TypeError, ValueError):
        decoded = {}
    if not isinstance(decoded, dict):
        decoded = {}
    # Legacy retained turns predate the snapshot; fail closed rather than using
    # potentially more permissive environment values during retry.
    return {
        "prompts": decoded.get("prompts") is True,
        "tool_details": decoded.get("tool_details") is True,
        "tool_content": decoded.get("tool_content") is True,
    }


def _handle_stop(input_json: dict) -> None:
    """Serialize and export one completed turn at most once."""
    state = resolve_session(input_json)
    with _operation_lock(state, "lifecycle"):
        _handle_stop_locked(state, input_json)


_REBUILD_STOP_BOUNDARY = object()


def _handle_stop_locked(state, input_json: dict) -> None:
    """Export the newest completed boundary while the session lock is held."""
    while _handle_stop_boundary_once(state, input_json) is _REBUILD_STOP_BOUNDARY:
        pass


def _handle_stop_boundary_once(state, input_json: dict):
    """Attempt one boundary snapshot; ask the caller to rebuild if it goes stale."""
    session_id = state.get("session_id")
    trace_id = state.get("current_trace_id")
    # Empty strings can survive in state files written by older versions; treat
    # them as absent so a cleared turn is never re-exported with a blank trace.
    if not session_id or not trace_id:
        return

    trace_span_id = state.get("current_trace_span_id") or generate_span_id()
    trace_start_time = state.get("current_trace_start_time") or str(get_timestamp_ms())
    privacy = _turn_privacy(state)
    user_prompt = state.get("current_trace_prompt") or ""
    project_name = state.get("project_name") or ""
    trace_count = state.get("trace_count") or "0"
    user_id = state.get("user_id") or ""
    ended_at = get_timestamp_ms()

    failed = state.get("turn_failed") == "1"
    root_event = TurnEvent(
        event_id=f"turn-{trace_id}",
        session_id=session_id,
        turn_id=trace_id,
        sequence=0,
        started_at_ms=int(trace_start_time),
        ended_at_ms=ended_at,
        status=EventStatus.FAILED if failed else EventStatus.COMPLETED,
        error=(input_json.get("error") or input_json.get("message") or "Turn failed") if failed else None,
        input=user_prompt,
        output=input_json.get("last_assistant_message", "") or "",
    )

    transcript = resolve_transcript_path(input_json, session_id)
    if transcript is None:
        log("Stop: no transcript available; retaining turn for retry")
        return

    start_line = int(state.get("trace_start_line") or "0")
    incoming_assistant_message = input_json.get("last_assistant_message", "") or ""
    synthetic_boundary = incoming_assistant_message.startswith("(Turn closed by fail-safe:")
    expected_assistant_message = "" if synthetic_boundary else incoming_assistant_message
    incoming_assistant_sha256 = (
        hashlib.sha256(expected_assistant_message.encode("utf-8")).hexdigest() if expected_assistant_message else ""
    )
    if privacy["prompts"]:
        if incoming_assistant_sha256:
            if state.set("expected_assistant_sha256", incoming_assistant_sha256) is False:
                log("Stop: failed to persist authoritative marker; retaining turn")
                return
        expected_assistant_sha256 = state.get("expected_assistant_sha256") or ""
    else:
        # The digest is itself a dictionary-testable content fingerprint. Keep it
        # in memory for this stability check, but remove any stale durable value.
        if state.get("expected_assistant_sha256") and state.set("expected_assistant_sha256", "") is False:
            log("Stop: failed to clear private authoritative marker; retaining turn")
            return
        expected_assistant_sha256 = incoming_assistant_sha256
    snapshot = _wait_for_stable_transcript(
        transcript,
        start_line,
        expected_assistant_message,
        expected_assistant_sha256,
        session_id,
        synthetic_boundary,
    )
    if snapshot is None:
        log("Stop: final assistant transcript record is not durable; retaining turn for retry")
        return
    graph = parse_qwen_transcript(transcript, root_event, start_line=start_line, transcript_text=snapshot)

    if any(not descriptor.get("ended_at_ms") for descriptor in _pending_subagents(state).values()):
        log("Stop: subagent still running; retaining turn for retry")
        return
    if not _merge_subagents(state, graph, input_json, parent_transcript=transcript):
        log("Stop: child transcript is not durable; retaining turn for retry")
        return

    root_attrs = {
        "session.id": session_id,
        "trace.number": trace_count,
        "project.name": project_name,
        "input.value": redact_content(privacy["prompts"], user_prompt),
    }
    model = state.get("model") or ""
    if model:
        root_attrs["llm.model_name"] = model
    if user_id:
        root_attrs["user.id"] = user_id

    # Span IDs are persisted so a retried export reuses the same identities
    # instead of duplicating the turn under fresh IDs.
    span_id_overrides = {root_event.event_id: trace_span_id}
    stored = state.get("qwen_span_ids") or ""
    if stored:
        try:
            decoded = json.loads(stored)
            if isinstance(decoded, dict):
                span_id_overrides.update(
                    {str(k): str(v) for k, v in decoded.items() if isinstance(k, str) and isinstance(v, str) and v}
                )
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log(f"invalid qwen_span_ids state; regenerating span IDs: {exc}")
    for event in graph.events:
        span_id_overrides.setdefault(event.event_id, generate_span_id())
    if state.set("qwen_span_ids", json.dumps(span_id_overrides, sort_keys=True)) is False:
        log("Stop: failed to persist stable span IDs; retaining turn")
        return

    payload = render_event_graph(
        graph,
        trace_id=trace_id,
        service_name=SERVICE_NAME,
        scope_name=SCOPE_NAME,
        span_id_overrides=span_id_overrides,
        extra_attributes={root_event.event_id: root_attrs},
        privacy_policy=privacy,
    )
    turn_revision = state.get("turn_revision") or "0"

    def boundary_fingerprint(revision: str) -> str:
        material = json.dumps(
            {
                "trace_id": trace_id,
                "turn_revision": revision,
                "transcript_line_count": len(snapshot.splitlines()),
                "events": [[event.event_id, event.status.value] for event in graph.events],
                "failed": failed,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    delivery_fingerprint = boundary_fingerprint(turn_revision)
    delivery_state = state.get("export_delivery_state") or ""
    if delivery_state.startswith("attempt:"):
        # Same-session boundaries must never overlap in transport: a newer Stop
        # cannot replace the lease that SessionEnd uses to account for an older
        # sender. Wait without the shared shard, then re-read durable state.
        _wait_for_live_delivery_lease(state)
        delivery_state = state.get("export_delivery_state") or ""
    if (state.get("turn_revision") or "0") != turn_revision:
        log("Stop: structural revision changed while waiting for delivery; rebuilding boundary")
        return _REBUILD_STOP_BOUNDARY
    if delivery_state == f"sent:{delivery_fingerprint}":
        state.set("turn_exported", "1")
        log("Stop: identical durable boundary already delivered")
        return
    if delivery_state.startswith("sent:"):
        # A prior boundary for this logical turn was accepted. A structurally
        # different continuation needs a fresh durable revision so retries
        # deduplicate against the new boundary rather than the old one.
        turn_revision = _next_turn_revision(state)
        if state.set_many({"turn_revision": turn_revision, "turn_exported": "0"}) is False:
            log("Stop: failed to persist structural boundary revision; retaining turn")
            return
        delivery_fingerprint = boundary_fingerprint(turn_revision)
    if delivery_state.startswith("attempt:") and _delivery_lease_is_held(state):
        log("Stop: prior durable boundary still has a live delivery lease")
        return
    attempt_prefix = f"attempt:{delivery_fingerprint}:"
    attempt = f"{attempt_prefix}{os.getpid()}"
    delivery_lock = FileLock(_delivery_lock_path(state), timeout=0.0, break_on_timeout=False)
    try:
        delivery_lock.__enter__()
    except TimeoutError:
        log("Stop: delivery transport lock is held; retaining turn")
        return
    try:
        if state.set_many({"export_delivery_state": attempt, "turn_exported": "0"}) is False:
            log("Stop: failed to persist delivery lease; retaining turn")
            return
        operation_lock = getattr(state, "_active_qwen_operation_lock", None)
        if operation_lock is None:
            delivered = send_span(payload)
        else:
            with operation_lock.suspended():
                delivered = send_span(payload)
        if state.get("current_trace_id") != trace_id or state.get("export_delivery_state") != attempt:
            log("Stop: lifecycle changed while transport was in flight; retaining newer state")
            return
        if delivered is False:
            state.set("export_delivery_state", f"failed:{delivery_fingerprint}")
            return
        if (state.get("turn_revision") or "0") != turn_revision:
            # A subagent producer committed after this boundary was captured.
            # Record the successful old delivery, but keep the turn open so the
            # revised ledger is exported before a new prompt may clear it.
            if state.set_many({"export_delivery_state": f"sent:{delivery_fingerprint}", "turn_exported": "0"}) is False:
                log("Stop: failed to retain late producer boundary")
            else:
                log("Stop: late producer arrived during transport; retaining turn for retry")
            return

        # A successful Stop export is not yet a definitive lifecycle boundary:
        # another blocking Stop hook may feed a continuation back to Qwen and
        # cause Stop to fire again for the same logical turn. Keep the captured
        # turn and stable identities so a later Stop updates the same trace.
        if (
            state.set_many(
                {
                    "export_delivery_state": f"sent:{delivery_fingerprint}",
                    "turn_exported": "1",
                }
            )
            is False
        ):
            log("Stop: failed to persist delivered boundary; retaining turn for stable-ID retry")
            return
        _periodic_gc(trace_count)
    finally:
        delivery_lock.__exit__(None, None, None)


def _handle_stop_failure(input_json: dict) -> None:
    """A turn that ended in failure still deserves a span; mark it as such."""
    state = resolve_session(input_json)
    with _operation_lock(state, "lifecycle"):
        if state.get("current_trace_id") is None:
            return
        state.set("turn_failed", "1")
        _handle_stop_locked(state, input_json)


def _clear_turn(state) -> None:
    """Drop per-turn state once a turn has been exported."""
    for key in (
        "current_trace_id",
        "current_trace_span_id",
        "current_trace_start_time",
        "current_trace_prompt",
        "current_trace_privacy",
        "trace_start_line",
        "qwen_span_ids",
        "expected_assistant_sha256",
        "pending_subagents",
        "turn_revision",
        "turn_failed",
        "turn_exported",
        "export_delivery_state",
    ):
        state.delete(key)


def _periodic_gc(trace_count: str) -> None:
    """Sweep stale PID-keyed state files every tenth turn."""
    try:
        if int(trace_count) % 10 == 0:
            gc_stale_state_files()
    except (TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# Subagents
# ---------------------------------------------------------------------------


def _next_turn_revision(state) -> str:
    try:
        return str(int(state.get("turn_revision") or "0") + 1)
    except (TypeError, ValueError):
        return "1"


def _handle_subagent_start(input_json: dict) -> None:
    """Record that a subagent began, keyed by the id Qwen assigns it."""
    state = resolve_session(input_json)
    agent_id = input_json.get("agent_id") or ""
    if not agent_id:
        return
    with _operation_lock(state, "lifecycle"):
        if not state.get("current_trace_id") or state.get("session_closed") == "1":
            log(f"SubagentStart: no active turn for {agent_id}; ignoring delayed event")
            return
        pending = _pending_subagents(state)
        pending[agent_id] = {
            "agent_id": agent_id,
            "agent_type": input_json.get("agent_type") or "",
            "input": redact_content(_turn_privacy(state)["prompts"], input_json.get("prompt") or ""),
            "started_at_ms": get_timestamp_ms(),
        }
        if (
            state.set_many(
                {
                    "pending_subagents": json.dumps(pending, sort_keys=True),
                    "turn_revision": _next_turn_revision(state),
                    "turn_exported": "0",
                }
            )
            is False
        ):
            log(f"SubagentStart: failed to persist ledger for {agent_id}")


def _handle_subagent_stop(input_json: dict) -> None:
    """Record the finished subagent along with the transcript Qwen wrote for it.

    Claude Code marks subagent records inline in the parent transcript with
    ``isSidechain``. Qwen Code instead writes the child to its own file and
    hands the path over here, so the child is parsed separately at Stop.
    """
    state = resolve_session(input_json)
    agent_id = input_json.get("agent_id") or ""
    if not agent_id:
        return
    with _operation_lock(state, "lifecycle"):
        if not state.get("current_trace_id") or state.get("session_closed") == "1":
            log(f"SubagentStop: no active turn for {agent_id}; ignoring delayed event")
            return
        pending = _pending_subagents(state)
        entry = pending.get(agent_id, {"agent_id": agent_id})
        raw_output = input_json.get("last_assistant_message") or ""
        privacy = _turn_privacy(state)
        raw_transcript_path = input_json.get("agent_transcript_path") or ""
        validated_path = resolve_agent_transcript_path(input_json) if raw_transcript_path else None
        persist_child_path = any(privacy.values())
        # Paths are metadata too. Persist only Qwen's canonical ID-bound path,
        # and only when at least one logging category is enabled for this turn.
        durable_path = str(validated_path) if validated_path is not None and persist_child_path else ""
        entry.update(
            {
                "agent_type": input_json.get("agent_type") or entry.get("agent_type") or "",
                "ended_at_ms": get_timestamp_ms(),
                "transcript_path": durable_path,
                "transcript_missing": "1" if raw_transcript_path and validated_path is None else "0",
                "output": redact_content(privacy["prompts"] and privacy["tool_content"], raw_output),
                "output_sha256": (
                    hashlib.sha256(raw_output.encode("utf-8")).hexdigest()
                    if raw_output and privacy["prompts"] and privacy["tool_content"]
                    else ""
                ),
            }
        )
        pending[agent_id] = entry
        if (
            state.set_many(
                {
                    "pending_subagents": json.dumps(pending, sort_keys=True),
                    "turn_revision": _next_turn_revision(state),
                    "turn_exported": "0",
                }
            )
            is False
        ):
            log(f"SubagentStop: failed to persist ledger for {agent_id}")


def _pending_subagents(state) -> dict:
    """Decode the pending-subagent ledger, tolerating malformed state."""
    raw = state.get("pending_subagents") or ""
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _tool_call_id_from_agent_id(agent_id: str, agent_type: str) -> str:
    """Recover the invoking tool call id from a Qwen subagent id.

    Qwen Code composes it as ``<agent_type>-<tool_call_id>``, e.g.
    ``general-purpose-call_jhze7894``. The agent type is not a safe delimiter on
    its own — it contains hyphens — so strip the known prefix and fall back to
    the ``call_`` marker when the type is missing or does not match.
    """
    if agent_type and agent_id.startswith(f"{agent_type}-"):
        return agent_id[len(agent_type) + 1 :]
    marker = agent_id.find("call_")
    return agent_id[marker:] if marker != -1 else ""


def _merge_subagents(state, graph, input_json: dict, parent_transcript=None) -> bool:
    """Attach subagents and, when distinct, their child transcript events.

    Subagents are bound to the invoking `agent` tool through the call id that
    Qwen embeds in `agent_id`, not through event order: with two subagents, or
    with `run_in_background: true`, SubagentStart/Stop need not arrive in the
    same order as the calls appear in the transcript, and ordinal pairing would
    file the span under the wrong invocation.
    """
    pending = _pending_subagents(state)
    if not pending:
        return True
    complete = True

    root = graph.events[0]
    # Tool calls that invoked a subagent: the parser tags them with the
    # subagent name taken from the task_execution result display.
    tools_by_call_id = {
        e.tool_call_id: e for e in graph.events if isinstance(e, ToolEvent) and e.tool_call_id and e.source_id
    }
    sequence = max((e.sequence for e in graph.events), default=0) + 1

    for descriptor in sorted(pending.values(), key=lambda d: d.get("started_at_ms") or 0):
        agent_id = descriptor.get("agent_id") or ""
        if not agent_id:
            continue
        agent_type = descriptor.get("agent_type") or ""

        call_id = _tool_call_id_from_agent_id(agent_id, agent_type)
        parent_tool = tools_by_call_id.get(call_id)
        if parent_tool is None:
            # An unmatched subagent still deserves a span; hanging it off the
            # turn keeps the evidence rather than dropping it.
            log(f"subagent {agent_id} has no matching tool call; attaching to the turn")
        parent_event_id = parent_tool.event_id if parent_tool is not None else root.event_id

        ended_at_ms = descriptor.get("ended_at_ms")
        agent_event = AgentEvent(
            event_id=f"agent-{agent_id}",
            session_id=root.session_id,
            turn_id=root.turn_id,
            parent_event_id=parent_event_id,
            agent_id=agent_id,
            source_id=agent_type,
            sequence=sequence,
            started_at_ms=descriptor.get("started_at_ms"),
            ended_at_ms=ended_at_ms,
            status=EventStatus.COMPLETED if ended_at_ms is not None else EventStatus.RUNNING,
            input=descriptor.get("input") or "",
            output=descriptor.get("output") or "",
        )
        graph.events.append(agent_event)
        sequence += 1

        child_path_value = descriptor.get("transcript_path") or ""
        if descriptor.get("transcript_missing") == "1":
            log(f"subagent {agent_id} declared an unavailable transcript")
            complete = False
            continue
        if not child_path_value or parent_transcript is None:
            continue
        child_input = dict(input_json)
        child_input["agent_id"] = agent_id
        child_input["agent_transcript_path"] = child_path_value
        child_path = resolve_agent_transcript_path(child_input, root.session_id, agent_id)
        if child_path is None:
            log(f"subagent {agent_id} transcript path rejected: {child_path_value}")
            complete = False
            continue
        try:
            is_separate_child = child_path != Path(parent_transcript).resolve()
        except OSError:
            is_separate_child = child_path.absolute() != Path(parent_transcript).absolute()
        if not is_separate_child:
            continue
        child_snapshot = None
        if child_path.is_file():
            child_snapshot = _wait_for_stable_transcript(
                child_path,
                0,
                expected_assistant_sha256=descriptor.get("output_sha256") or "",
            )
        if child_snapshot is None:
            log(f"subagent {agent_id} transcript unavailable or incomplete: {child_path}")
            complete = False
            continue

        child_graph = parse_qwen_transcript(child_path, agent_event, start_line=0, transcript_text=child_snapshot)
        graph.events.extend(child_graph.events[1:])
        graph.diagnostics.extend(child_graph.diagnostics)
        sequence = max((event.sequence for event in graph.events), default=sequence - 1) + 1

    return complete


# ---------------------------------------------------------------------------
# Tools, todos and advisory events
# ---------------------------------------------------------------------------


def _handle_pre_tool_use(input_json: dict) -> None:
    """Count tool starts. The transcript remains the authoritative record."""
    state = resolve_session(input_json)
    with _operation_lock(state, "lifecycle"):
        ensure_session_initialized(state, input_json)
        state.increment("tool_count")


def _handle_post_tool_use(input_json: dict) -> None:
    """No-op beyond diagnostics: tool results are read from the transcript."""
    log(f"tool completed: {input_json.get('tool_name') or 'unknown'}")


def _handle_post_tool_use_failure(input_json: dict) -> None:
    """Log a tool failure; the transcript carries error and errorType."""
    log(f"tool failed: {input_json.get('tool_name') or 'unknown'}")


def _handle_todo_created(input_json: dict) -> None:
    """Count created todos, once per item.

    Todo hooks fire twice per item — once in the ``validation`` phase, which
    exists to block the write, and once in ``postWrite``. Counting both would
    double every todo, so only the post-write phase is durable.
    """
    if input_json.get("phase") != TODO_PHASE_POSTWRITE:
        return
    state = resolve_session(input_json)
    with _operation_lock(state, "lifecycle"):
        state.increment("todo_created_count")


def _handle_todo_completed(input_json: dict) -> None:
    """Count completed todos, once per item. See _handle_todo_created."""
    if input_json.get("phase") != TODO_PHASE_POSTWRITE:
        return
    state = resolve_session(input_json)
    with _operation_lock(state, "lifecycle"):
        state.increment("todo_completed_count")


def _handle_pre_compact(input_json: dict) -> None:
    """Note an imminent context compaction."""
    log("context compaction starting")


def _handle_post_compact(input_json: dict) -> None:
    """Note a completed context compaction."""
    log("context compaction finished")


def _handle_notification(input_json: dict) -> None:
    """Log a user-facing notification."""
    log(f"notification: {input_json.get('notification_type') or ''} {input_json.get('message') or ''}".strip())


def _handle_permission_request(input_json: dict) -> None:
    """Log a permission prompt."""
    log(f"permission requested for {input_json.get('tool_name') or 'unknown'}")


def _handle_permission_denied(input_json: dict) -> None:
    """Log a denied permission."""
    log(f"permission denied for {input_json.get('tool_name') or 'unknown'}")


# ---------------------------------------------------------------------------
# CLI entry points
# ---------------------------------------------------------------------------


def _entry(name: str, handler) -> None:
    """Run *handler* against stdin, never propagating a failure to the host."""
    if not env.trace_enabled:
        return
    os.environ.setdefault("ARIZE_LOG_FILE", str(HARNESSES["qwen"]["default_log_file"]))
    redirect_stderr_to_log_file()
    try:
        handler(_read_stdin())
    except Exception as exc:  # noqa: BLE001 - hooks must not break the CLI
        error(f"{name} hook failed: {exc}")


def session_start() -> None:
    _entry("session_start", _handle_session_start)


def session_end() -> None:
    _entry("session_end", _handle_session_end)


def user_prompt_submit() -> None:
    _entry("user_prompt_submit", _handle_user_prompt_submit)


def pre_tool_use() -> None:
    _entry("pre_tool_use", _handle_pre_tool_use)


def post_tool_use() -> None:
    _entry("post_tool_use", _handle_post_tool_use)


def post_tool_use_failure() -> None:
    _entry("post_tool_use_failure", _handle_post_tool_use_failure)


def stop() -> None:
    _entry("stop", _handle_stop)


def stop_failure() -> None:
    _entry("stop_failure", _handle_stop_failure)


def subagent_start() -> None:
    _entry("subagent_start", _handle_subagent_start)


def subagent_stop() -> None:
    _entry("subagent_stop", _handle_subagent_stop)


def pre_compact() -> None:
    _entry("pre_compact", _handle_pre_compact)


def post_compact() -> None:
    _entry("post_compact", _handle_post_compact)


def notification() -> None:
    _entry("notification", _handle_notification)


def permission_request() -> None:
    _entry("permission_request", _handle_permission_request)


def permission_denied() -> None:
    _entry("permission_denied", _handle_permission_denied)


def todo_created() -> None:
    _entry("todo_created", _handle_todo_created)


def todo_completed() -> None:
    _entry("todo_completed", _handle_todo_completed)
