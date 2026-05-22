"""Tests for verbose-mode config wiring (config.yaml + _is_verbose() interaction)."""

from __future__ import annotations

import pytest
import yaml

from core import common


@pytest.fixture
def tmp_config(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE to a tmp path and provide a writer."""
    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("core.constants.CONFIG_FILE", str(config_path))
    monkeypatch.setattr("core.config.CONFIG_FILE", str(config_path))

    def write(data: dict) -> None:
        config_path.write_text(yaml.safe_dump(data))

    return write


def test_verbose_false_by_default(tmp_config, monkeypatch):
    monkeypatch.delenv("ARIZE_VERBOSE", raising=False)
    tmp_config({})
    assert common._is_verbose() is False


def test_verbose_true_from_config(tmp_config, monkeypatch):
    monkeypatch.delenv("ARIZE_VERBOSE", raising=False)
    tmp_config({"verbose": True})
    assert common._is_verbose() is True


def test_env_var_overrides_config_true(tmp_config, monkeypatch):
    monkeypatch.setenv("ARIZE_VERBOSE", "true")
    tmp_config({"verbose": False})
    assert common._is_verbose() is True


def test_env_var_overrides_config_false(tmp_config, monkeypatch):
    monkeypatch.setenv("ARIZE_VERBOSE", "false")
    tmp_config({"verbose": True})
    assert common._is_verbose() is False


def test_write_verbose_config_creates_key(tmp_config, monkeypatch):
    from core.setup import write_verbose_config

    tmp_config({})
    write_verbose_config(True)
    from core.config import load_config

    cfg = load_config()
    assert cfg.get("verbose") is True


def test_write_verbose_config_writes_false(tmp_config, monkeypatch):
    from core.setup import write_verbose_config

    tmp_config({})
    write_verbose_config(False)
    from core.config import load_config

    cfg = load_config()
    assert cfg.get("verbose") is False


def test_write_verbose_config_preserves_other_keys(tmp_config, monkeypatch):
    from core.config import load_config
    from core.setup import write_verbose_config

    tmp_config(
        {
            "harnesses": {"claude-code": {"project_name": "claude-code"}},
            "user_id": "alice",
        }
    )
    write_verbose_config(True)

    cfg = load_config()
    assert cfg.get("verbose") is True
    assert cfg.get("user_id") == "alice"
    assert cfg["harnesses"]["claude-code"]["project_name"] == "claude-code"


def test_write_verbose_config_overwrites_existing_value(tmp_config, monkeypatch):
    from core.config import load_config
    from core.setup import write_verbose_config

    tmp_config({"verbose": True})
    write_verbose_config(False)
    assert load_config().get("verbose") is False


def test_write_verbose_config_creates_missing_file(tmp_path, monkeypatch):
    """If config.yaml does not yet exist, write_verbose_config creates it."""
    from core.config import load_config
    from core.setup import write_verbose_config

    config_path = tmp_path / "config.yaml"
    monkeypatch.setattr("core.constants.CONFIG_FILE", str(config_path))
    monkeypatch.setattr("core.config.CONFIG_FILE", str(config_path))

    assert not config_path.exists()
    write_verbose_config(True)
    assert config_path.exists()
    assert load_config().get("verbose") is True


def test_empty_env_var_falls_back_to_config(tmp_config, monkeypatch):
    """ARIZE_VERBOSE='' (empty) should not force off — fall back to config."""
    monkeypatch.setenv("ARIZE_VERBOSE", "")
    tmp_config({"verbose": True})
    assert common._is_verbose() is True


def test_env_var_alt_truthy_values(tmp_config, monkeypatch):
    """ARIZE_VERBOSE=1, yes, YES, True should all be truthy."""
    tmp_config({})
    for val in ("1", "yes", "YES", "True", "TRUE", "Yes"):
        monkeypatch.setenv("ARIZE_VERBOSE", val)
        assert common._is_verbose() is True, f"value {val!r} should be truthy"


def test_env_var_alt_falsy_values(tmp_config, monkeypatch):
    """ARIZE_VERBOSE=0, no, NO, False should all be falsy and override config."""
    tmp_config({"verbose": True})
    for val in ("0", "no", "NO", "False", "FALSE", "No"):
        monkeypatch.setenv("ARIZE_VERBOSE", val)
        assert common._is_verbose() is False, f"value {val!r} should be falsy"


def test_unrecognized_env_var_falls_back_to_config(tmp_config, monkeypatch):
    """An unrecognized env-var value falls through to the config check."""
    monkeypatch.setenv("ARIZE_VERBOSE", "maybe")
    tmp_config({"verbose": True})
    assert common._is_verbose() is True


def test_env_verbose_property_reads_config(tmp_config, monkeypatch):
    """The _Env.verbose property must use the new _is_verbose() logic."""
    from core.common import env

    monkeypatch.delenv("ARIZE_VERBOSE", raising=False)
    tmp_config({"verbose": True})
    assert env.verbose is True


def test_env_verbose_property_env_override(tmp_config, monkeypatch):
    """_Env.verbose: explicit env-var false should override config true."""
    from core.common import env

    monkeypatch.setenv("ARIZE_VERBOSE", "false")
    tmp_config({"verbose": True})
    assert env.verbose is False


def test_is_verbose_handles_malformed_config(tmp_path, monkeypatch):
    """If the config file is unreadable/malformed, _is_verbose() returns False."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(": : : not yaml")  # malformed
    monkeypatch.setattr("core.constants.CONFIG_FILE", str(config_path))
    monkeypatch.setattr("core.config.CONFIG_FILE", str(config_path))
    monkeypatch.delenv("ARIZE_VERBOSE", raising=False)
    assert common._is_verbose() is False


def test_write_verbose_config_dry_run(tmp_config, monkeypatch):
    """dry_run mode should NOT mutate the config file."""
    from core.config import load_config
    from core.setup import write_verbose_config

    tmp_config({})
    monkeypatch.setenv("ARIZE_DRY_RUN", "true")
    write_verbose_config(True)
    # File should not have been changed
    cfg = load_config()
    assert "verbose" not in cfg


def test_write_verbose_config_explicit_path(tmp_path, monkeypatch):
    """When a config_path is passed, write_verbose_config uses it."""
    from core.config import load_config
    from core.setup import write_verbose_config

    monkeypatch.delenv("ARIZE_DRY_RUN", raising=False)
    config_path = tmp_path / "custom.yaml"
    write_verbose_config(True, config_path=str(config_path))
    assert config_path.exists()
    assert load_config(str(config_path)).get("verbose") is True
