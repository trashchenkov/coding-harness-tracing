#!/usr/bin/env python3
"""End-to-end packaging tests for the Cursor marketplace plugin.

Validates that the manifests, hook configuration, packaging metadata, core
symlink, and bootstrap script all cohere as described in the design spec
(`docs/superpowers/specs/2026-06-08-cursor-marketplace-plugin-design.md`).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - fallback for older Python
    tomllib = None  # type: ignore[assignment]

REPO_ROOT = Path(__file__).parent.parent.parent.parent

# Sanity-check the path traversal — this file lives at
# tests/tracing/cursor/test_cursor_plugin.py, so four parents up is the repo
# root.
assert (REPO_ROOT / "pyproject.toml").exists(), f"REPO_ROOT misresolved: {REPO_ROOT} has no pyproject.toml"


PLUGIN_DIR = REPO_ROOT / "tracing" / "cursor"
MARKETPLACE_JSON = REPO_ROOT / ".cursor-plugin" / "marketplace.json"
PLUGIN_JSON = PLUGIN_DIR / ".cursor-plugin" / "plugin.json"
HOOKS_JSON = PLUGIN_DIR / "hooks" / "hooks.json"
PYPROJECT = PLUGIN_DIR / "pyproject.toml"
CORE_SYMLINK = PLUGIN_DIR / "core"
RUN_HOOK = PLUGIN_DIR / "scripts" / "run-hook"


def _install_source_hash(root=PLUGIN_DIR):
    files = [root / "pyproject.toml"]
    seen_dirs = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real_dir = os.path.realpath(dirpath)
        if real_dir in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(real_dir)
        dirnames[:] = sorted(name for name in dirnames if name != "__pycache__")
        files.extend(Path(dirpath) / name for name in sorted(filenames) if name.endswith(".py"))

    digest = hashlib.sha256()
    for path in sorted(set(files), key=lambda value: os.path.relpath(value, root)):
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        digest.update(relative.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


# --- marketplace.json ---


class TestMarketplaceJson:
    """Registry entry at .cursor-plugin/marketplace.json."""

    @pytest.fixture
    def data(self):
        with open(MARKETPLACE_JSON) as f:
            return json.load(f)

    def test_is_valid_json(self, data):
        assert isinstance(data, dict)

    def test_has_required_fields(self, data):
        assert "name" in data
        assert "plugins" in data
        assert isinstance(data["plugins"], list)
        assert data["plugins"], "plugins[] must not be empty"

    def test_first_plugin_source(self, data):
        assert data["plugins"][0]["source"] == "./tracing/cursor"

    def test_first_plugin_name(self, data):
        assert data["plugins"][0]["name"] == "cursor-tracing"


# --- plugin.json ---


class TestPluginJson:
    """Plugin manifest at tracing/cursor/.cursor-plugin/plugin.json."""

    @pytest.fixture
    def data(self):
        with open(PLUGIN_JSON) as f:
            return json.load(f)

    def test_is_valid_json(self, data):
        assert isinstance(data, dict)

    def test_has_required_fields(self, data):
        assert "name" in data
        assert "description" in data
        assert "version" in data

    def test_name_matches_distribution(self, data):
        assert data["name"] == "cursor-tracing"

    def test_hooks_path(self, data):
        assert data["hooks"] == "hooks/hooks.json"

    def test_hooks_file_referenced_by_manifest_exists(self, data):
        hooks_path = (PLUGIN_DIR / data["hooks"]).resolve()
        assert hooks_path.exists(), f"hooks file from plugin.json missing: {hooks_path}"


# --- hooks.json ---


class TestHooksJson:
    """Cursor hook map at tracing/cursor/hooks/hooks.json."""

    @pytest.fixture
    def data(self):
        with open(HOOKS_JSON) as f:
            return json.load(f)

    def test_is_valid_json(self, data):
        assert isinstance(data, dict)
        assert data["version"] == 1
        assert "hooks" in data

    def test_current_official_event_inventory(self, data):
        """Cursor 2.5+ exposes 21 documented hook events."""
        assert len(data["hooks"]) == 21
        assert {
            "preToolUse",
            "postToolUseFailure",
            "subagentStart",
            "subagentStop",
            "preCompact",
            "workspaceOpen",
        } <= set(data["hooks"])

    def test_event_keys_match_constants(self, data):
        from tracing.cursor.constants import HOOK_EVENTS

        assert set(data["hooks"].keys()) == set(HOOK_EVENTS)

    def _iter_commands(self, data):
        for event, hook_list in data["hooks"].items():
            for entry in hook_list:
                yield event, entry["command"]

    def test_every_command_uses_run_hook(self, data):
        # Commands must be anchored to the plugin install dir via
        # ${CURSOR_PLUGIN_ROOT}: Cursor runs plugin hook commands from the
        # opened *project* folder, not the plugin dir, so a relative
        # "./scripts/run-hook" resolves to <project>/scripts/run-hook and
        # silently no-ops. The script still self-resolves PLUGIN_ROOT from $0
        # as a fallback once it is found.
        expected = "${CURSOR_PLUGIN_ROOT}/scripts/run-hook"
        for event, cmd in self._iter_commands(data):
            assert cmd == expected, f"{event}: expected {expected!r}, got {cmd!r}"

    def test_no_forbidden_tokens_in_commands(self, data):
        for event, cmd in self._iter_commands(data):
            assert ".sh" not in cmd, f"{event}: command contains '.sh': {cmd}"
            assert "bash" not in cmd, f"{event}: command contains 'bash': {cmd}"
            assert "~/.arize" not in cmd, f"{event}: command contains '~/.arize': {cmd}"


# --- pyproject.toml ---


class TestPyproject:
    """Plugin-local pyproject at tracing/cursor/pyproject.toml."""

    def test_is_real_file_not_symlink(self):
        assert PYPROJECT.exists()
        assert not PYPROJECT.is_symlink(), (
            "tracing/cursor/pyproject.toml must be a real file, not a symlink — "
            "a symlinked pyproject materializes to repo-root content whose "
            "package layout references source outside the plugin root, "
            "breaking marketplace installs."
        )

    @pytest.fixture
    def parsed(self):
        if tomllib is None:
            pytest.skip("tomllib not available (Python < 3.11)")
        with open(PYPROJECT, "rb") as f:
            return tomllib.load(f)

    def test_distribution_name_is_distinct(self, parsed):
        # Must differ from repo-root dist name to avoid two distributions
        # owning the same files in one venv (silent overwrite).
        assert parsed["project"]["name"] == "cursor-tracing"

    def test_entry_point_for_cursor(self, parsed):
        scripts = parsed["project"]["scripts"]
        assert scripts["arize-hook-cursor"] == "tracing.cursor.hooks.handlers:main"

    def test_setuptools_packages_exact_set(self, parsed):
        packages = set(parsed["tool"]["setuptools"]["packages"])
        # Exact set: guards against the #41 vscode_bridge trap where listing
        # a core.* dir that only has gitignored files breaks marketplace
        # installs.
        assert packages == {
            "tracing.cursor",
            "tracing.cursor.hooks",
            "core",
            "core.setup",
        }


# --- core symlink ---


class TestCoreSymlink:
    """The tracing/cursor/core path must be a symlink to ../../core."""

    def test_is_symlink(self):
        assert CORE_SYMLINK.is_symlink(), f"{CORE_SYMLINK} must be a symlink to ../../core"

    def test_symlink_target(self):
        target = os.readlink(CORE_SYMLINK)
        assert target == "../../core", f"unexpected symlink target: {target!r}"

    def test_symlink_resolves(self):
        assert (REPO_ROOT / "tracing/cursor/core/common.py").exists()


# --- run-hook script ---


class TestRunHook:
    """Bootstrap script at tracing/cursor/scripts/run-hook."""

    def test_exists(self):
        assert RUN_HOOK.is_file()

    def test_is_executable(self):
        assert os.access(RUN_HOOK, os.X_OK)

    def test_has_sh_shebang(self):
        first_line = RUN_HOOK.read_text().splitlines()[0]
        assert first_line.startswith("#!/bin/sh"), f"expected POSIX sh shebang, got: {first_line!r}"

    def test_references_cursor_entry_point(self):
        assert "arize-hook-cursor" in RUN_HOOK.read_text()

    def test_uses_dedicated_venv_path(self):
        # Must be plugin-dedicated, NOT the shared bare harness/venv used by
        # install.sh — otherwise a marketplace install would clobber an
        # install.sh-managed venv (and vice versa).
        text = RUN_HOOK.read_text()
        assert "cursor-plugin-venv" in text

    def test_resolves_plugin_root_from_script(self):
        # The manifest uses CURSOR_PLUGIN_ROOT; after host expansion (and in
        # direct smoke tests) the bootstrap derives its root from its own path.
        assert "dirname" in RUN_HOOK.read_text()

    def test_does_not_reference_claude_plugin_vars(self):
        text = RUN_HOOK.read_text()
        assert "CLAUDE_PLUGIN_ROOT" not in text
        assert "CLAUDE_PLUGIN_DATA" not in text

    def test_sh_syntax_check_passes(self):
        if not shutil.which("sh"):
            pytest.skip("sh not available")
        result = subprocess.run(
            ["sh", "-n", str(RUN_HOOK)],
            capture_output=True,
        )
        assert result.returncode == 0, f"sh -n syntax check failed: {result.stderr.decode(errors='replace')}"

    def test_parallel_first_fire_bootstraps_once(self, tmp_path):
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        count_file = tmp_path / "install-count"
        entry_template = tmp_path / "entry"
        entry_template.write_text("#!/bin/sh\ncat >/dev/null\nprintf '{}'\n")
        entry_template.chmod(0o755)
        pip_template = tmp_path / "pip"
        pip_template.write_text(
            "#!/bin/sh\n"
            'printf x >> "$BOOTSTRAP_COUNT"\n'
            "/usr/bin/sleep 0.3\n"
            'dest="$HOME/.arize/harness/cursor-plugin-venv/bin/arize-hook-cursor"\n'
            '/usr/bin/cp "$FAKE_ENTRY" "$dest"\n'
            '/usr/bin/chmod +x "$dest"\n'
        )
        pip_template.chmod(0o755)
        python = fake_bin / "python3"
        python.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = -c ]; then\n'
            '  case "$2" in\n'
            "    *version_info*) exit 0 ;;\n"
            "    *hashlib*) /usr/bin/sha256sum \"$3\" | /usr/bin/cut -d' ' -f1; exit 0 ;;\n"
            "  esac\n"
            "fi\n"
            'if [ "$1" = -m ] && [ "$2" = venv ]; then\n'
            '  /usr/bin/mkdir -p "$3/bin"\n'
            '  /usr/bin/cp "$FAKE_PIP" "$3/bin/pip"\n'
            '  /usr/bin/chmod +x "$3/bin/pip"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = - ]; then exec /usr/bin/python3 "$@"; fi\n'
            "exit 1\n"
        )
        python.chmod(0o755)
        env = {
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "BOOTSTRAP_COUNT": str(count_file),
            "FAKE_ENTRY": str(entry_template),
            "FAKE_PIP": str(pip_template),
        }

        processes = [
            subprocess.Popen(
                [str(RUN_HOOK)], env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            for _ in range(2)
        ]
        results = [process.communicate(b"{}", timeout=30) for process in processes]

        assert all(process.returncode == 0 for process in processes)
        assert [stdout for stdout, _ in results] == [b"{}", b"{}"]
        assert count_file.read_text() == "x"

    def test_reinstalls_when_installable_source_changes(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        shutil.copytree(PLUGIN_DIR, plugin_root)
        run_hook = plugin_root / "scripts" / "run-hook"
        home = tmp_path / "home"
        venv_bin = home / ".arize" / "harness" / "cursor-plugin-venv" / "bin"
        venv_bin.mkdir(parents=True)
        entry_point = venv_bin / "arize-hook-cursor"
        pip = venv_bin / "pip"
        pip.write_text(
            "#!/bin/sh\n"
            'BIN_DIR=$(dirname "$0")\n'
            "printf '#!/bin/sh\\nprintf NEW_ARTIFACT\\n' > \"$BIN_DIR/arize-hook-cursor\"\n"
            'chmod +x "$BIN_DIR/arize-hook-cursor"\n'
        )
        pip.chmod(0o755)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        python = fake_bin / "python3"
        python.write_text(
            "#!/bin/sh\n" 'if [ "$1" = -m ] && [ "$2" = venv ]; then exit 0; fi\n' 'exec /usr/bin/python3 "$@"\n'
        )
        python.chmod(0o755)
        env = {**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"}

        first = subprocess.run([str(run_hook)], env=env, input=b"{}", capture_output=True, timeout=10)
        assert first.returncode == 0
        assert first.stdout == b"NEW_ARTIFACT"
        marker = home / ".arize" / "harness" / ".cursor-plugin.pyproject.sha256"
        assert marker.read_text().strip() == _install_source_hash(plugin_root)

        entry_point.write_text("#!/bin/sh\nprintf OLD_ARTIFACT\n")
        entry_point.chmod(0o755)
        handlers = plugin_root / "hooks" / "handlers.py"
        handlers.write_text(handlers.read_text() + "\n# source update\n")

        second = subprocess.run([str(run_hook)], env=env, input=b"{}", capture_output=True, timeout=10)
        assert second.returncode == 0
        assert second.stdout == b"NEW_ARTIFACT"
        assert marker.read_text().strip() == _install_source_hash(plugin_root)

    def test_source_change_during_install_is_detected_on_next_invocation(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        shutil.copytree(PLUGIN_DIR, plugin_root)
        run_hook = plugin_root / "scripts" / "run-hook"
        handlers = plugin_root / "hooks" / "handlers.py"
        home = tmp_path / "home"
        data_dir = home / ".arize" / "harness"
        venv_bin = data_dir / "cursor-plugin-venv" / "bin"
        venv_bin.mkdir(parents=True)
        count_file = tmp_path / "install-count"
        pip = venv_bin / "pip"
        pip.write_text(
            "#!/bin/sh\n"
            'BIN_DIR=$(dirname "$0")\n'
            f"if [ ! -f '{count_file}' ]; then\n"
            f"  printf x > '{count_file}'\n"
            f"  printf '\\n# source changed during install\\n' >> '{handlers}'\n"
            "  printf '#!/bin/sh\\nprintf STALE_ARTIFACT\\n' > \"$BIN_DIR/arize-hook-cursor\"\n"
            "else\n"
            f"  printf x >> '{count_file}'\n"
            "  printf '#!/bin/sh\\nprintf FRESH_ARTIFACT\\n' > \"$BIN_DIR/arize-hook-cursor\"\n"
            "fi\n"
            'chmod +x "$BIN_DIR/arize-hook-cursor"\n'
        )
        pip.chmod(0o755)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        python = fake_bin / "python3"
        python.write_text(
            "#!/bin/sh\n" 'if [ "$1" = -m ] && [ "$2" = venv ]; then exit 0; fi\n' 'exec /usr/bin/python3 "$@"\n'
        )
        python.chmod(0o755)
        env = {**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"}

        first = subprocess.run([str(run_hook)], env=env, input=b"{}", capture_output=True, timeout=10)
        second = subprocess.run([str(run_hook)], env=env, input=b"{}", capture_output=True, timeout=10)

        marker = data_dir / ".cursor-plugin.pyproject.sha256"
        assert first.returncode == second.returncode == 0
        assert first.stdout == b"STALE_ARTIFACT"
        assert second.stdout == b"FRESH_ARTIFACT"
        assert count_file.read_text() == "xx"
        assert marker.read_text().strip() == _install_source_hash(plugin_root)

    def test_terminating_signal_does_not_publish_marker_or_execute_artifact(self, tmp_path):
        plugin_root = tmp_path / "plugin"
        shutil.copytree(PLUGIN_DIR, plugin_root)
        run_hook = plugin_root / "scripts" / "run-hook"
        home = tmp_path / "home"
        venv_bin = home / ".arize" / "harness" / "cursor-plugin-venv" / "bin"
        venv_bin.mkdir(parents=True)
        pip = venv_bin / "pip"
        pip.write_text(
            "#!/bin/sh\n"
            'kill -TERM "$PPID"\n'
            "/usr/bin/sleep 0.1\n"
            'BIN_DIR=$(dirname "$0")\n'
            "printf '#!/bin/sh\\nprintf EXECUTED_AFTER_TERM\\n' > \"$BIN_DIR/arize-hook-cursor\"\n"
            'chmod +x "$BIN_DIR/arize-hook-cursor"\n'
        )
        pip.chmod(0o755)
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        python = fake_bin / "python3"
        python.write_text(
            "#!/bin/sh\n" 'if [ "$1" = -m ] && [ "$2" = venv ]; then exit 0; fi\n' 'exec /usr/bin/python3 "$@"\n'
        )
        python.chmod(0o755)

        result = subprocess.run(
            [str(run_hook)],
            env={**os.environ, "HOME": str(home), "PATH": f"{fake_bin}:{os.environ['PATH']}"},
            input=b"{}",
            capture_output=True,
            timeout=10,
        )

        data_dir = home / ".arize" / "harness"
        assert result.returncode == 0
        assert result.stdout == b""
        assert not (data_dir / ".cursor-plugin.pyproject.sha256").exists()
        assert not (data_dir / ".cursor-plugin-bootstrap.lock").exists()

    @pytest.mark.parametrize("pid_contents", [None, "", "not-a-pid"])
    def test_recovers_lock_with_missing_or_malformed_pid(self, tmp_path, pid_contents):
        home = tmp_path / "home"
        data_dir = home / ".arize" / "harness"
        venv_bin = data_dir / "cursor-plugin-venv" / "bin"
        venv_bin.mkdir(parents=True)
        pip = venv_bin / "pip"
        pip.write_text("#!/bin/sh\nexit 0\n")
        pip.chmod(0o755)
        marker = data_dir / ".cursor-plugin.pyproject.sha256"
        marker.write_text(_install_source_hash())
        lock_dir = data_dir / ".cursor-plugin-bootstrap.lock"
        lock_dir.mkdir()
        if pid_contents is not None:
            (lock_dir / "pid").write_text(pid_contents)

        result = subprocess.run(
            [str(RUN_HOOK)],
            env={**os.environ, "HOME": str(home)},
            input=b"{}",
            capture_output=True,
            timeout=4,
        )

        assert result.returncode == 0
        assert result.stdout == b""
        assert not lock_dir.exists()

    def test_fails_open_with_empty_stdout_when_bootstrap_fails(self):
        """If no Python is available, run-hook must exit 0 with empty stdout.

        Cursor interprets stdout as control JSON, so a tracing hook must
        never emit anything there even when its bootstrap can't proceed.
        """
        if not shutil.which("sh"):
            pytest.skip("sh not available")

        with tempfile.TemporaryDirectory() as tmp:
            # PATH set so no python/python3/py can be found; HOME redirected
            # so any cached venv from a prior run isn't picked up.
            env = {
                "HOME": tmp,
                "PATH": tmp,  # empty dir → no python on PATH
            }
            result = subprocess.run(
                [str(RUN_HOOK)],
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=30,
            )
        assert result.returncode == 0, (
            f"run-hook must fail open (exit 0); got {result.returncode}. "
            f"stderr: {result.stderr.decode(errors='replace')}"
        )
        assert result.stdout == b"", f"run-hook must write nothing to stdout; got {result.stdout!r}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
