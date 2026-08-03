"""Regression tests for the Qwen Code hook installer."""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event, Lock, Thread

import pytest

from tracing.qwen import constants, install


@pytest.fixture
def settings_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "qwen home" / "settings.json"
    monkeypatch.setattr(constants, "SETTINGS_DIR", path.parent)
    monkeypatch.setattr(constants, "SETTINGS_FILE", path)
    monkeypatch.setattr(install, "dry_run", lambda: False)
    monkeypatch.setattr(install, "venv_bin", lambda entry_point: Path("/opt/arize runtime/bin") / entry_point)
    return path


def _shared_block() -> dict:
    return {
        "matcher": "",
        "hooks": [
            {
                "type": "command",
                "name": constants.HOOK_NAME,
                "command": "/old/arize-hook",
            },
            {
                "type": "command",
                "name": "keep-me",
                "command": "/usr/local/bin/user-hook",
            },
        ],
    }


def test_settings_path_follows_qwen_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    qwen_home = tmp_path / "relocated-qwen"
    monkeypatch.setenv("QWEN_HOME", str(qwen_home))

    assert install._settings_dir() == qwen_home
    assert install._settings_file() == qwen_home / "settings.json"


def test_settings_path_falls_back_when_qwen_home_is_unset(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    default_dir = tmp_path / ".qwen"
    monkeypatch.delenv("QWEN_HOME", raising=False)
    monkeypatch.setattr(constants, "SETTINGS_DIR", default_dir)
    monkeypatch.setattr(constants, "SETTINGS_FILE", default_dir / "settings.json")

    assert install._settings_dir() == default_dir
    assert install._settings_file() == default_dir / "settings.json"


def test_read_settings_accepts_qwen_jsonc(settings_file: Path) -> None:
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        """{
  // Qwen Code accepts comments in settings.json.
  "theme": "// remains part of the string",
  "pattern": "/* also remains part of the string */",
  "escaped": "quote: \\" // remains; slash: \\\\ /* remains",
  /* and block comments */
  "hooks": {}
}
""",
        encoding="utf-8",
    )

    assert install._read_settings() == {
        "theme": "// remains part of the string",
        "pattern": "/* also remains part of the string */",
        "escaped": 'quote: " // remains; slash: \\ /* remains',
        "hooks": {},
    }


def test_read_settings_accepts_bom_comments_and_trailing_commas(settings_file: Path) -> None:
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        '\ufeff{\n  // valid Qwen JSONC\n  "theme": "dark",\n  "hooks": {\n    "Stop": [],\n  },\n}\n',
        encoding="utf-8",
    )

    assert install._read_settings() == {"theme": "dark", "hooks": {"Stop": []}}


@pytest.mark.parametrize(
    "payload, expected_error",
    [
        ([], "root must be a JSON object"),
        ({"hooks": None}, "'hooks' must be a JSON object"),
        ({"hooks": []}, "'hooks' must be a JSON object"),
        ({"hooks": {"SessionStart": {}}}, "hooks.SessionStart must be a JSON array"),
    ],
)
def test_install_rejects_malformed_settings_schema(
    settings_file: Path, payload: object, expected_error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_file.parent.mkdir(parents=True)
    original = json.dumps(payload) + "\n"
    settings_file.write_text(original, encoding="utf-8")

    with pytest.raises(SystemExit) as exc_info:
        install._install_hooks()

    assert exc_info.value.code == 1
    assert expected_error in capsys.readouterr().err
    assert settings_file.read_text(encoding="utf-8") == original


def test_install_preserves_other_hooks_in_same_matcher_block(settings_file: Path) -> None:
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        json.dumps({"hooks": {"SessionStart": [_shared_block()]}}),
        encoding="utf-8",
    )

    install._install_hooks()

    blocks = json.loads(settings_file.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
    hooks = [hook for block in blocks for hook in block.get("hooks", [])]
    assert sum(hook.get("name") == constants.HOOK_NAME for hook in hooks) == 1
    assert any(hook.get("name") == "keep-me" for hook in hooks)


def test_uninstall_preserves_other_hooks_in_same_matcher_block(settings_file: Path) -> None:
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        json.dumps({"hooks": {"SessionStart": [_shared_block()]}}),
        encoding="utf-8",
    )

    install._uninstall_hooks()

    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data == {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "name": "keep-me",
                            "command": "/usr/local/bin/user-hook",
                        }
                    ],
                }
            ]
        }
    }


def test_hook_command_quotes_runtime_paths_with_spaces(settings_file: Path) -> None:
    install._install_hooks()

    data = json.loads(settings_file.read_text(encoding="utf-8"))
    command = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert command == "'/opt/arize runtime/bin/arize-hook-qwen-session-start'"


def test_hook_command_uses_windows_quoting() -> None:
    command = install._quote_command(r"C:\Program Files\Arize\arize-hook-qwen-session-start.exe", platform_name="nt")

    assert command == '"C:\\Program Files\\Arize\\arize-hook-qwen-session-start.exe"'


def test_install_aborts_if_settings_change_during_read_modify_write(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text('{"theme": "before"}\n', encoding="utf-8")
    real_write = install._write_settings

    def external_writer_wins(data, **kwargs):
        settings_file.write_text('{"theme": "external"}\n', encoding="utf-8")
        return real_write(data, **kwargs)

    monkeypatch.setattr(install, "_write_settings", external_writer_wins)

    with pytest.raises(SystemExit) as exc_info:
        install._install_hooks()

    assert exc_info.value.code == 1
    assert "changed while Qwen hooks were being updated" in capsys.readouterr().err
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"theme": "external"}


def test_concurrent_installs_leave_one_owned_hook_per_event(settings_file: Path) -> None:
    errors: list[BaseException] = []

    def run_install() -> None:
        try:
            install._install_hooks()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [Thread(target=run_install) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    hooks_map = json.loads(settings_file.read_text(encoding="utf-8"))["hooks"]
    for blocks in hooks_map.values():
        hooks = [hook for block in blocks for hook in block.get("hooks", [])]
        assert sum(hook.get("name") == constants.HOOK_NAME for hook in hooks) == 1


def test_concurrent_install_then_uninstall_preserves_foreign_hook(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(
        json.dumps({"hooks": {"SessionStart": [_shared_block()]}}),
        encoding="utf-8",
    )
    entered_write = Event()
    release_write = Event()
    call_lock = Lock()
    first_call = True
    real_write = install._write_settings

    def block_first_write(data, **kwargs):
        nonlocal first_call
        with call_lock:
            should_block = first_call
            first_call = False
        if should_block:
            entered_write.set()
            assert release_write.wait(timeout=5)
        return real_write(data, **kwargs)

    monkeypatch.setattr(install, "_write_settings", block_first_write)
    errors: list[BaseException] = []

    def run(operation) -> None:
        try:
            operation()
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    installing = Thread(target=run, args=(install._install_hooks,))
    uninstalling = Thread(target=run, args=(install._uninstall_hooks,))
    installing.start()
    assert entered_write.wait(timeout=5)
    uninstalling.start()
    release_write.set()
    installing.join(timeout=5)
    uninstalling.join(timeout=5)

    assert not errors
    assert not installing.is_alive()
    assert not uninstalling.is_alive()
    data = json.loads(settings_file.read_text(encoding="utf-8"))
    assert data == {
        "hooks": {
            "SessionStart": [
                {
                    "matcher": "",
                    "hooks": [
                        {
                            "type": "command",
                            "name": "keep-me",
                            "command": "/usr/local/bin/user-hook",
                        }
                    ],
                }
            ]
        }
    }


def test_write_settings_atomically_replaces_existing_file(settings_file: Path) -> None:
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text('{"old": true}\n', encoding="utf-8")
    original_inode = settings_file.stat().st_ino

    install._write_settings({"new": True})

    assert settings_file.stat().st_ino != original_inode
    assert json.loads(settings_file.read_text(encoding="utf-8")) == {"new": True}


def test_write_settings_uses_same_directory_temporary_file(
    settings_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = {}
    real_named_temporary_file = install.tempfile.NamedTemporaryFile

    def recording_named_temporary_file(*args, **kwargs):
        captured["dir"] = kwargs.get("dir")
        return real_named_temporary_file(*args, **kwargs)

    monkeypatch.setattr(install.tempfile, "NamedTemporaryFile", recording_named_temporary_file)

    install._write_settings({"hooks": {}})

    assert Path(captured["dir"]) == settings_file.parent


def test_write_settings_preserves_existing_mode(settings_file: Path) -> None:
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text("{}\n", encoding="utf-8")
    settings_file.chmod(0o640)

    install._write_settings({"hooks": {}})

    assert settings_file.stat().st_mode & 0o777 == 0o640


def test_write_settings_uses_private_mode_for_new_file(settings_file: Path) -> None:
    install._write_settings({"hooks": {}})

    assert settings_file.stat().st_mode & 0o777 == 0o600
