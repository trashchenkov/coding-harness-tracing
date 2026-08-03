import hashlib
import json
import re
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from core.common import StateManager
from tracing.qwen.hooks import adapter, handlers


def _assistant(text: str, session_id: str = "sess-1") -> str:
    return json.dumps(
        {
            "uuid": f"assistant-{text}",
            "sessionId": session_id,
            "type": "assistant",
            "timestamp": "2026-08-03T00:00:01Z",
            "message": {"role": "assistant", "parts": [{"text": text}]},
        }
    )


def _payload(**overrides) -> dict:
    payload = {
        "session_id": "sess-1",
        "cwd": "/tmp/project",
        "prompt": "",
        "submitted_prompt": "",
        "transcript_path": "",
    }
    payload.update(overrides)
    return payload


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(handlers, "STATE_DIR", tmp_path, raising=False)
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path)
    monkeypatch.setattr(adapter, "PROJECTS_DIR", tmp_path)

    def transcript(payload, _session_id=None, *, must_exist=True):
        raw = payload.get("transcript_path") or ""
        return Path(raw) if raw else None

    monkeypatch.setattr(handlers, "resolve_transcript_path", transcript)
    sent = []
    monkeypatch.setattr(handlers, "send_span", lambda payload: sent.append(payload) or True)
    return sent


def test_qwen_runtime_dir_environment_value_is_literal(tmp_path, monkeypatch):
    process_cwd = tmp_path / "process-cwd"
    process_cwd.mkdir()
    monkeypatch.chdir(process_cwd)
    monkeypatch.setenv("QWEN_RUNTIME_DIR", "${SECOND}/runtime")
    monkeypatch.setenv("SECOND", str(tmp_path / "expanded-by-adapter"))

    assert adapter._runtime_base_dir(str(tmp_path / "payload-cwd")) == (process_cwd / "${SECOND}" / "runtime").resolve()


def test_qwen_runtime_dir_named_user_tilde_is_literal(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("QWEN_RUNTIME_DIR", "~root/qwen-probe")

    assert adapter._runtime_base_dir(str(tmp_path / "payload-cwd")) == (tmp_path / "~root" / "qwen-probe").resolve()


def test_namespace_key_rejects_intermediate_symlink_without_side_effects(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(adapter, "STATE_DIR", tmp_path / "link" / "qwen")

    with pytest.raises(OSError):
        adapter._state_namespace_key()

    assert not (outside / "qwen").exists()


def test_transcript_resolver_rejects_symlink_below_runtime_anchor(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    outside = tmp_path / "outside"
    workspace = tmp_path / "workspace"
    outside.mkdir()
    workspace.mkdir()
    monkeypatch.setenv("QWEN_RUNTIME_DIR", str(runtime))

    session_id = "sess-secret"
    project = adapter._project_dir_for_cwd(str(workspace))
    project.mkdir(parents=True)
    (project / "chats").symlink_to(outside, target_is_directory=True)
    victim = outside / f"{session_id}.jsonl"
    victim.write_text(_assistant("EXTERNAL-SENTINEL", session_id) + "\n", encoding="utf-8")

    payload = {
        "session_id": session_id,
        "cwd": str(workspace),
        "transcript_path": str(project / "chats" / victim.name),
    }
    assert adapter.resolve_transcript_path(payload) is None


def test_full_opt_out_state_namespace_is_keyed_and_hides_cwd_basename(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    runtime = tmp_path / "runtime"
    workspace = tmp_path / "sensitive-project-name"
    workspace.mkdir()
    monkeypatch.setattr(adapter, "STATE_DIR", state_dir)
    monkeypatch.setenv("QWEN_RUNTIME_DIR", str(runtime))
    for name in ("ARIZE_LOG_PROMPTS", "ARIZE_LOG_TOOL_DETAILS", "ARIZE_LOG_TOOL_CONTENT"):
        monkeypatch.setenv(name, "false")
    for name in ("ARIZE_PROJECT_NAME", "ARIZE_QWEN_PROJECT_NAME"):
        monkeypatch.delenv(name, raising=False)

    payload = {"session_id": "guessable-session", "cwd": str(workspace)}
    state = adapter.resolve_session(payload)
    state.init_state()
    adapter.ensure_session_initialized(state, payload)

    identity = f"guessable-session\0{workspace.resolve()}\0{runtime.resolve()}"
    plain_digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    assert state.state_file is not None
    state_file = state.state_file
    assert state_file.name != f"state_session_{plain_digest}.json"
    assert "sensitive-project-name" not in state_file.read_text(encoding="utf-8")
    namespace_key = state_dir / ".namespace_key"
    assert namespace_key.stat().st_mode & 0o777 == 0o600
    assert state_dir.stat().st_mode & 0o777 == 0o700


def test_state_set_many_persists_one_complete_mapping(tmp_path):
    state = StateManager(tmp_path, tmp_path / "state.json", tmp_path / ".lock")
    state.init_state()
    assert state.set_many({"first": "1", "second": "2"}) is True
    assert state.get("first") == "1"
    assert state.get("second") == "2"


def test_session_end_exports_newer_boundary_after_prior_stop(isolated, tmp_path):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    start = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(start)
    transcript.write_text(_assistant("first boundary") + "\n", encoding="utf-8")
    handlers._handle_stop(_payload(last_assistant_message="first boundary", transcript_path=str(transcript)))
    transcript.write_text(transcript.read_text() + _assistant("continuation final") + "\n", encoding="utf-8")

    handlers._handle_session_end(_payload(hook_event_name="SessionEnd", transcript_path=str(transcript)))

    assert len(isolated) == 2
    assert "continuation final" in json.dumps(isolated[-1])


def test_prompt_start_never_follows_replacement_symlink(isolated, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    outside = tmp_path / "outside.txt"
    transcript.write_text("", encoding="utf-8")
    outside.write_text("{}\n" * 7, encoding="utf-8")

    def swapped(_payload, _session_id=None, *, must_exist=True):
        transcript.unlink()
        transcript.symlink_to(outside)
        return transcript

    monkeypatch.setattr(handlers, "resolve_transcript_path", swapped)
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)

    assert adapter.resolve_session(payload).get("current_trace_id") is None
    assert adapter.resolve_session(payload).get("trace_start_line") is None


def test_other_session_is_not_blocked_by_network_send_on_same_shard(isolated, tmp_path, monkeypatch):
    monkeypatch.setattr(adapter, "LOCK_SHARD_COUNT", 1)
    first_path = tmp_path / "first.jsonl"
    second_path = tmp_path / "second.jsonl"
    first_path.write_text("", encoding="utf-8")
    second_path.write_text("", encoding="utf-8")
    first = _payload(session_id="first", prompt="one", transcript_path=str(first_path))
    second = _payload(session_id="second", prompt="two", transcript_path=str(second_path))
    handlers._handle_user_prompt_submit(first)
    first_path.write_text(_assistant("done", "first") + "\n", encoding="utf-8")
    entered = threading.Event()
    second_entered = threading.Event()
    release = threading.Event()
    send_count = 0
    send_count_lock = threading.Lock()

    def blocked_first_send(_payload):
        nonlocal send_count
        with send_count_lock:
            send_count += 1
            ordinal = send_count
        if ordinal == 1:
            entered.set()
            release.wait(timeout=3)
        else:
            second_entered.set()
        return True

    monkeypatch.setattr(handlers, "send_span", blocked_first_send)
    worker = threading.Thread(
        target=handlers._handle_stop,
        args=(_payload(session_id="first", last_assistant_message="done", transcript_path=str(first_path)),),
    )
    worker.start()
    assert entered.wait(timeout=1)

    handlers._handle_user_prompt_submit(second)
    second_path.write_text(_assistant("done", "second") + "\n", encoding="utf-8")
    second_done = threading.Event()

    def stop_second():
        try:
            handlers._handle_stop(
                _payload(session_id="second", last_assistant_message="done", transcript_path=str(second_path))
            )
        finally:
            second_done.set()

    second_worker = threading.Thread(target=stop_second)
    second_worker.start()
    try:
        assert second_entered.wait(timeout=0.5)
        assert second_done.wait(timeout=0.5)
        assert adapter.resolve_session(second).get("turn_exported") == "1"
    finally:
        release.set()
        worker.join(timeout=2)
        second_worker.join(timeout=2)


def test_turn_batch_failure_leaves_no_partial_trace(isolated, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    original = StateManager.set_many
    calls = 0

    def fail_turn_batch(self, values):
        nonlocal calls
        calls += 1
        if "current_trace_id" in values:
            return False
        return original(self, values)

    monkeypatch.setattr(StateManager, "set_many", fail_turn_batch)
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)

    state = adapter.resolve_session(payload)
    assert calls >= 2
    assert state.get("current_trace_id") is None
    assert state.get("current_trace_prompt") is None


def test_stale_delivery_lease_with_reused_live_pid_is_reclaimed(isolated, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)
    transcript.write_text(_assistant("done") + "\n", encoding="utf-8")

    def crash_after_lease(_payload):
        raise RuntimeError("simulated process crash before transport completion")

    monkeypatch.setattr(handlers, "send_span", crash_after_lease)
    with pytest.raises(RuntimeError):
        handlers._handle_stop(_payload(last_assistant_message="done", transcript_path=str(transcript)))
    state = adapter.resolve_session(payload)
    stable_ids = state.get("qwen_span_ids")
    lease = state.get("export_delivery_state") or ""
    assert lease.startswith("attempt:")
    state.set("export_delivery_state", lease.rsplit(":", 1)[0] + f":{__import__('os').getpid()}")
    sent = []
    monkeypatch.setattr(handlers, "send_span", lambda exported: sent.append(exported) or True)

    handlers._handle_stop(_payload(last_assistant_message="done", transcript_path=str(transcript)))

    assert len(sent) == 1
    assert state.get("qwen_span_ids") == stable_ids
    assert state.get("turn_exported") == "1"


def test_state_namespace_prevents_cross_cwd_prompt_export(isolated, tmp_path):
    a_path = tmp_path / "a.jsonl"
    b_path = tmp_path / "b.jsonl"
    a_path.write_text("", encoding="utf-8")
    b_path.write_text(_assistant("b output") + "\n", encoding="utf-8")
    start = _payload(cwd="/project/a", prompt="A-SECRET", transcript_path=str(a_path))
    handlers._handle_user_prompt_submit(start)

    handlers._handle_stop(_payload(cwd="/project/b", last_assistant_message="b output", transcript_path=str(b_path)))

    assert isolated == []
    assert adapter.resolve_session(start).get("current_trace_prompt") == "A-SECRET"


def test_session_end_waits_across_replaced_live_lease(monkeypatch):
    deliveries = iter(
        [
            f"attempt:first:{__import__('os').getpid()}",
            f"attempt:second:{__import__('os').getpid()}",
            f"attempt:second:{__import__('os').getpid()}",
            "sent:second",
        ]
    )
    observed = []

    class Lock:
        @contextmanager
        def suspended(self):
            yield

    class State:
        _active_qwen_operation_lock = Lock()

        def get(self, _key):
            value = next(deliveries)
            observed.append(value)
            return value

    monkeypatch.setattr(handlers.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(handlers, "_delivery_lease_is_held", lambda _state: True)
    handlers._wait_for_live_delivery_lease(State())

    assert observed[-1] == "sent:second"


def test_runtime_output_setting_accepts_qwen_jsonc(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    qwen_home = tmp_path / "home"
    settings = qwen_home / "settings.json"
    settings.parent.mkdir(parents=True)
    runtime = tmp_path / "runtime"
    settings.write_text(
        f"""{{
  // Qwen settings are JSONC.
  "advanced": {{"runtimeOutputDir": {json.dumps(str(runtime))},}},
}}""",
        encoding="utf-8",
    )
    project_id = re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))
    transcript = runtime / "projects" / project_id / "chats" / "sess-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.delenv("QWEN_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))

    assert (
        adapter.resolve_transcript_path({"session_id": "sess-1", "cwd": str(cwd), "transcript_path": str(transcript)})
        == transcript
    )


def test_newer_stop_cannot_overlap_prior_session_transport(isolated, tmp_path, monkeypatch):
    transcript = tmp_path / "chat.jsonl"
    transcript.write_text("", encoding="utf-8")
    payload = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(payload)

    started = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]
    sends = []

    def blocked_send(span):
        index = len(sends)
        sends.append(span)
        started[index].set()
        assert release[index].wait(timeout=3)
        return True

    monkeypatch.setattr(handlers, "send_span", blocked_send)
    transcript.write_text(_assistant("first") + "\n", encoding="utf-8")
    first = threading.Thread(
        target=handlers._handle_stop,
        args=(_payload(last_assistant_message="first", transcript_path=str(transcript)),),
    )
    first.start()
    assert started[0].wait(timeout=1)

    transcript.write_text(transcript.read_text() + _assistant("second") + "\n", encoding="utf-8")
    second = threading.Thread(
        target=handlers._handle_stop,
        args=(_payload(last_assistant_message="second", transcript_path=str(transcript)),),
    )
    second.start()
    assert not started[1].wait(timeout=0.1)

    release[0].set()
    assert started[1].wait(timeout=1)
    ended = threading.Event()

    def end_session():
        handlers._handle_session_end(_payload(hook_event_name="SessionEnd", transcript_path=str(transcript)))
        ended.set()

    session_end = threading.Thread(target=end_session)
    session_end.start()
    assert not ended.wait(timeout=0.1)
    release[1].set()
    for thread in (first, second, session_end):
        thread.join(timeout=2)
        assert not thread.is_alive()

    assert len(sends) == 2
    assert adapter.resolve_session(payload).get("session_closed") == "1"


def test_runtime_settings_merge_system_env_and_defaults(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    qwen_home = tmp_path / "home"
    system = tmp_path / "etc" / "settings.json"
    defaults = tmp_path / "etc" / "system-defaults.json"
    for path, value in (
        (defaults, "defaults-runtime"),
        (qwen_home / "settings.json", "user-runtime"),
        (cwd / ".qwen" / "settings.json", "workspace-runtime"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"advanced": {"runtimeOutputDir": value}}), encoding="utf-8")
    system.parent.mkdir(parents=True, exist_ok=True)
    system.write_text('{"advanced":{"runtimeOutputDir":"${RUNTIME_ROOT}/system"}}', encoding="utf-8")
    monkeypatch.setenv("QWEN_RUNTIME_DIR", "")
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_SETTINGS_PATH", str(system))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_DEFAULTS_PATH", str(defaults))
    monkeypatch.setenv("RUNTIME_ROOT", str(tmp_path))

    assert adapter._runtime_base_dir(str(cwd)) == tmp_path / "system"


def test_untrusted_workspace_runtime_setting_is_excluded(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    qwen_home = tmp_path / "home"
    system = tmp_path / "etc" / "settings.json"
    trusted = qwen_home / "trustedFolders.json"
    user = qwen_home / "settings.json"
    workspace = cwd / ".qwen" / "settings.json"
    for path in (system, trusted, user, workspace):
        path.parent.mkdir(parents=True, exist_ok=True)
    system.write_text("{}", encoding="utf-8")
    user.write_text(
        json.dumps(
            {
                "security": {"folderTrust": {"enabled": True}},
                "advanced": {"runtimeOutputDir": str(tmp_path / "user-runtime")},
            }
        ),
        encoding="utf-8",
    )
    workspace.write_text(
        json.dumps({"advanced": {"runtimeOutputDir": str(tmp_path / "workspace-runtime")}}),
        encoding="utf-8",
    )
    trusted.write_text(json.dumps({str(cwd): "DO_NOT_TRUST"}), encoding="utf-8")
    monkeypatch.delenv("QWEN_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_SETTINGS_PATH", str(system))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_DEFAULTS_PATH", str(tmp_path / "missing-defaults.json"))

    assert adapter._runtime_base_dir(str(cwd)) == tmp_path / "user-runtime"


def test_runtime_output_setting_uses_qwen_home_dotenv_fallback(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    qwen_home = tmp_path / "qwen-home"
    qwen_home.mkdir()
    dotenv_runtime = tmp_path / "from-dotenv"
    process_runtime = tmp_path / "from-process"
    (qwen_home / ".env").write_text(f"ONLY_IN_QWEN_DOTENV={dotenv_runtime}\n", encoding="utf-8")
    (qwen_home / "settings.json").write_text(
        '{"advanced":{"runtimeOutputDir":"${ONLY_IN_QWEN_DOTENV}/runs"}}', encoding="utf-8"
    )
    monkeypatch.delenv("QWEN_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_SETTINGS_PATH", str(tmp_path / "missing-system.json"))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_DEFAULTS_PATH", str(tmp_path / "missing-defaults.json"))
    monkeypatch.delenv("ONLY_IN_QWEN_DOTENV", raising=False)

    assert adapter._runtime_base_dir(str(cwd)) == dotenv_runtime / "runs"

    monkeypatch.setenv("ONLY_IN_QWEN_DOTENV", str(process_runtime))
    assert adapter._runtime_base_dir(str(cwd)) == process_runtime / "runs"


def test_qwen_home_dotenv_accepts_utf8_bom_on_first_assignment(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    qwen_home = tmp_path / "qwen-home"
    qwen_home.mkdir()
    target = tmp_path / "from-bom-dotenv"
    (qwen_home / ".env").write_text(f"\ufeffONLY_IN_QWEN_DOTENV={target}\n", encoding="utf-8")
    (qwen_home / "settings.json").write_text(
        '{"advanced":{"runtimeOutputDir":"${ONLY_IN_QWEN_DOTENV}/runs"}}', encoding="utf-8"
    )
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_SETTINGS_PATH", str(tmp_path / "missing-system.json"))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_DEFAULTS_PATH", str(tmp_path / "missing-defaults.json"))
    monkeypatch.delenv("QWEN_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("ONLY_IN_QWEN_DOTENV", raising=False)

    assert adapter._runtime_base_dir(str(cwd)) == target / "runs"


def test_runtime_output_setting_expands_environment_only_once(tmp_path, monkeypatch):
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    qwen_home = tmp_path / "qwen-home"
    qwen_home.mkdir()
    (qwen_home / ".env").write_text("ONLY_IN_QWEN_DOTENV=${SECOND_LEVEL}/dotenv\n", encoding="utf-8")
    (qwen_home / "settings.json").write_text(
        '{"advanced":{"runtimeOutputDir":"${ONLY_IN_QWEN_DOTENV}/runs"}}', encoding="utf-8"
    )
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))
    monkeypatch.setenv("SECOND_LEVEL", str(tmp_path / "must-not-expand"))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_SETTINGS_PATH", str(tmp_path / "missing-system.json"))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_DEFAULTS_PATH", str(tmp_path / "missing-defaults.json"))
    monkeypatch.delenv("QWEN_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("ONLY_IN_QWEN_DOTENV", raising=False)

    assert adapter._runtime_base_dir(str(cwd)) == (cwd / "${SECOND_LEVEL}" / "dotenv" / "runs").resolve()


def test_concurrent_distinct_stops_advance_structural_revision(isolated, tmp_path, monkeypatch):
    transcript = tmp_path / "concurrent-boundaries.jsonl"
    transcript.write_text("", encoding="utf-8")
    start = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(start)
    transcript.write_text(_assistant("first") + "\n", encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()

    def block_first(payload):
        isolated.append(payload)
        if len(isolated) == 1:
            entered.set()
            assert release.wait(timeout=3)
        return True

    monkeypatch.setattr(handlers, "send_span", block_first)
    first = threading.Thread(
        target=handlers._handle_stop,
        args=(_payload(last_assistant_message="first", transcript_path=str(transcript)),),
    )
    first.start()
    assert entered.wait(timeout=1)

    transcript.write_text(_assistant("first") + "\n" + _assistant("second") + "\n", encoding="utf-8")
    second = threading.Thread(
        target=handlers._handle_stop,
        args=(_payload(last_assistant_message="second", transcript_path=str(transcript)),),
    )
    second.start()
    release.set()
    first.join(timeout=3)
    second.join(timeout=3)

    assert not first.is_alive() and not second.is_alive()
    assert len(isolated) == 2
    assert adapter.resolve_session(start).get("turn_revision") == "1"


def test_failed_atomic_delivery_completion_remains_retryable_with_stable_ids(isolated, tmp_path, monkeypatch):
    transcript = tmp_path / "delivery-completion.jsonl"
    transcript.write_text("", encoding="utf-8")
    stop = _payload(prompt="build", last_assistant_message="done", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(stop)
    transcript.write_text(_assistant("done") + "\n", encoding="utf-8")
    state_type = type(adapter.resolve_session(stop))
    original_set = state_type.set
    original_set_many = state_type.set_many
    rejected_completion = False

    def reject_first_set_completion(state, key, value):
        nonlocal rejected_completion
        if not rejected_completion and key == "export_delivery_state" and value.startswith("sent:"):
            rejected_completion = True
            return False
        return original_set(state, key, value)

    def reject_first_atomic_completion(state, values):
        nonlocal rejected_completion
        if (
            not rejected_completion
            and values.get("export_delivery_state", "").startswith("sent:")
            and values.get("turn_exported") == "1"
        ):
            rejected_completion = True
            return False
        return original_set_many(state, values)

    monkeypatch.setattr(state_type, "set", reject_first_set_completion)
    monkeypatch.setattr(state_type, "set_many", reject_first_atomic_completion)

    handlers._handle_stop(stop)
    after_first = adapter.resolve_session(stop)
    assert rejected_completion is True
    assert len(isolated) == 1
    assert (after_first.get("export_delivery_state") or "").startswith("attempt:")
    assert after_first.get("turn_exported") == "0"

    handlers._handle_stop(stop)
    after_retry = adapter.resolve_session(stop)
    assert len(isolated) == 2
    first_span = isolated[0]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    retry_span = isolated[1]["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert retry_span["traceId"] == first_span["traceId"]
    assert retry_span["spanId"] == first_span["spanId"]
    assert (after_retry.get("export_delivery_state") or "").startswith("sent:")
    assert after_retry.get("turn_exported") == "1"


def test_three_overlapping_distinct_stops_export_the_newest_boundary(isolated, tmp_path, monkeypatch):
    transcript = tmp_path / "three-concurrent-boundaries.jsonl"
    transcript.write_text("", encoding="utf-8")
    start = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(start)

    first_send = threading.Event()
    second_send = threading.Event()
    release_first = threading.Event()
    release_second = threading.Event()
    second_waiting = threading.Event()
    third_waiting = threading.Event()
    release_third_wait = threading.Event()
    send_lock = threading.Lock()

    def controlled_send(payload):
        with send_lock:
            isolated.append(payload)
            ordinal = len(isolated)
        if ordinal == 1:
            first_send.set()
            assert release_first.wait(timeout=3)
        elif ordinal == 2:
            second_send.set()
            assert release_second.wait(timeout=3)
        return True

    monkeypatch.setattr(handlers, "send_span", controlled_send)
    original_wait = handlers._wait_for_live_delivery_lease
    third_wait_calls = 0

    def controlled_wait(state):
        nonlocal third_wait_calls
        name = threading.current_thread().name
        if name == "stop-2":
            second_waiting.set()
            return original_wait(state)
        if name == "stop-3":
            third_wait_calls += 1
            if third_wait_calls == 1:
                third_waiting.set()
                operation_lock = getattr(state, "_active_qwen_operation_lock")
                with operation_lock.suspended():
                    assert release_third_wait.wait(timeout=3)
                return None
        return original_wait(state)

    monkeypatch.setattr(handlers, "_wait_for_live_delivery_lease", controlled_wait)

    transcript.write_text(_assistant("boundary-1") + "\n", encoding="utf-8")
    first = threading.Thread(
        name="stop-1",
        target=handlers._handle_stop,
        args=(_payload(last_assistant_message="boundary-1", transcript_path=str(transcript)),),
    )
    first.start()
    assert first_send.wait(timeout=1)

    transcript.write_text(transcript.read_text() + _assistant("boundary-2") + "\n", encoding="utf-8")
    second = threading.Thread(
        name="stop-2",
        target=handlers._handle_stop,
        args=(_payload(last_assistant_message="boundary-2", transcript_path=str(transcript)),),
    )
    second.start()
    assert second_waiting.wait(timeout=1)

    transcript.write_text(transcript.read_text() + _assistant("boundary-3") + "\n", encoding="utf-8")
    third = threading.Thread(
        name="stop-3",
        target=handlers._handle_stop,
        args=(_payload(last_assistant_message="boundary-3", transcript_path=str(transcript)),),
    )
    third.start()
    assert third_waiting.wait(timeout=1)

    release_first.set()
    assert second_send.wait(timeout=2)
    release_third_wait.set()
    release_second.set()
    first.join(timeout=3)
    second.join(timeout=3)
    third.join(timeout=3)

    assert not first.is_alive() and not second.is_alive() and not third.is_alive()
    assert len(isolated) == 3
    assert "boundary-3" in json.dumps(isolated[-1])
    state = adapter.resolve_session(start)
    assert state.get("turn_revision") == "2"
    assert state.get("turn_exported") == "1"


def test_late_subagent_during_transport_is_exported_before_next_prompt(isolated, tmp_path, monkeypatch):
    transcript = tmp_path / "late-producer.jsonl"
    transcript.write_text("", encoding="utf-8")
    first_prompt = _payload(prompt="build", transcript_path=str(transcript))
    handlers._handle_user_prompt_submit(first_prompt)
    transcript.write_text(_assistant("done") + "\n", encoding="utf-8")

    entered = threading.Event()
    release = threading.Event()
    sends = []

    def block_first(payload):
        sends.append(payload)
        if len(sends) == 1:
            entered.set()
            assert release.wait(timeout=3)
        return True

    monkeypatch.setattr(handlers, "send_span", block_first)
    worker = threading.Thread(
        target=handlers._handle_stop,
        args=(_payload(last_assistant_message="done", transcript_path=str(transcript)),),
    )
    worker.start()
    assert entered.wait(timeout=1)

    handlers._handle_subagent_start(
        _payload(hook_event_name="SubagentStart", agent_id="late-agent", prompt="late work")
    )
    handlers._handle_subagent_stop(
        _payload(hook_event_name="SubagentStop", agent_id="late-agent", last_assistant_message="late result")
    )
    release.set()
    worker.join(timeout=2)
    assert not worker.is_alive()

    state = adapter.resolve_session(first_prompt)
    assert state.get("turn_exported") == "0"
    assert "late-agent" in (state.get("pending_subagents") or "")

    handlers._handle_user_prompt_submit(
        _payload(prompt="next", submitted_prompt="next", transcript_path=str(transcript))
    )

    assert len(sends) == 2
    assert "late-agent" in json.dumps(sends[1])
    assert adapter.resolve_session(first_prompt).get("current_trace_prompt") == "next"


def test_folder_trust_matches_qwen_versioned_legacy_migration():
    assert adapter._folder_trust_enabled({"security": {"other": True}, "folderTrust": True}, {}) is True
    assert adapter._folder_trust_enabled({"$version": 2, "folderTrust": True}, {}) is False
    assert (
        adapter._folder_trust_enabled(
            {"security": {"folderTrust": {"enabled": False}}, "folderTrust": True},
            {},
        )
        is False
    )


def test_empty_qwen_home_and_top_level_runtime_setting_fall_back_to_global(tmp_path, monkeypatch):
    qwen_home = tmp_path / ".qwen"
    qwen_home.mkdir()
    (qwen_home / "settings.json").write_text(
        json.dumps({"runtimeOutputDir": str(tmp_path / "legacy-runtime")}),
        encoding="utf-8",
    )
    monkeypatch.setattr(adapter, "PROJECTS_DIR", qwen_home / "projects")
    monkeypatch.setenv("QWEN_HOME", "")
    monkeypatch.delenv("QWEN_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("QWEN_CODE_SYSTEM_SETTINGS_PATH", str(tmp_path / "missing-system.json"))
    monkeypatch.setenv("QWEN_CODE_SYSTEM_DEFAULTS_PATH", str(tmp_path / "missing-defaults.json"))

    assert adapter._runtime_base_dir(str(tmp_path / "workspace")) == qwen_home


def test_resolver_accepts_workspace_runtime_output_dir(tmp_path, monkeypatch):
    workspace = tmp_path / "work.space"
    workspace.mkdir()
    monkeypatch.chdir(workspace)
    settings = workspace / ".qwen" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(json.dumps({"advanced": {"runtimeOutputDir": "runtime"}}), encoding="utf-8")
    transcript = (
        workspace / "runtime" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(workspace)) / "chats" / "sess-1.jsonl"
    )
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.delenv("QWEN_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("QWEN_HOME", str(tmp_path / "home"))

    resolved = adapter.resolve_transcript_path(
        {"session_id": "sess-1", "cwd": str(workspace), "transcript_path": str(transcript)}
    )

    assert resolved == transcript


def test_resolver_accepts_runtime_dir_and_exact_cwd_sanitizer(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    transcript = runtime / "projects" / "-tmp-work-space" / "chats" / "sess-1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setenv("QWEN_RUNTIME_DIR", str(runtime))

    resolved = adapter.resolve_transcript_path(
        {"session_id": "sess-1", "cwd": "/tmp/work.space", "transcript_path": str(transcript)}
    )

    assert resolved == transcript
