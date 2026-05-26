"""Tests for core/config.py — pure module functions for reading/writing config.yaml."""

import os
import stat

import pytest
import yaml

from core.config import delete_value, get_value, load_config, save_config, set_value


@pytest.fixture(autouse=True)
def _mock_sleep(monkeypatch):
    sleep_calls = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_calls.append(s))
    return sleep_calls


# ---------------------------------------------------------------------------
# Module API tests
# ---------------------------------------------------------------------------


class TestGetValue:
    def test_simple_key(self):
        assert get_value({"a": 1}, "a") == 1

    def test_nested_key(self):
        cfg = {"backend": {"target": "phoenix"}}
        assert get_value(cfg, "backend.target") == "phoenix"

    def test_missing_key(self):
        assert get_value({"a": 1}, "b") is None

    def test_missing_nested_key(self):
        assert get_value({"a": {"b": 1}}, "a.c") is None

    def test_partial_path_non_dict(self):
        assert get_value({"a": "scalar"}, "a.b") is None


class TestSetValue:
    def test_creates_nested_path(self):
        cfg = {}
        set_value(cfg, "a.b.c", 42)
        assert cfg == {"a": {"b": {"c": 42}}}

    def test_overwrites_existing(self):
        cfg = {"a": {"b": 1}}
        set_value(cfg, "a.b", 2)
        assert cfg["a"]["b"] == 2

    def test_creates_intermediate_dicts(self):
        cfg = {"a": "old"}
        set_value(cfg, "a.b", 1)
        assert cfg == {"a": {"b": 1}}

    def test_returns_config(self):
        cfg = {}
        result = set_value(cfg, "x", 1)
        assert result is cfg


class TestDeleteValue:
    def test_removes_key(self):
        cfg = {"a": 1, "b": 2}
        delete_value(cfg, "a")
        assert cfg == {"b": 2}

    def test_removes_nested_key(self):
        cfg = {"a": {"b": 1, "c": 2}}
        delete_value(cfg, "a.b")
        assert cfg == {"a": {"c": 2}}

    def test_missing_key_noop(self):
        cfg = {"a": 1}
        delete_value(cfg, "b")
        assert cfg == {"a": 1}

    def test_non_dict_parent(self):
        cfg = {"a": "scalar"}
        delete_value(cfg, "a.b")
        assert cfg == {"a": "scalar"}

    def test_returns_config(self):
        cfg = {"a": 1}
        result = delete_value(cfg, "a")
        assert result is cfg


# ---------------------------------------------------------------------------
# save_config / load_config
# ---------------------------------------------------------------------------


class TestSaveConfig:
    def test_writes_valid_yaml(self, tmp_path):
        path = tmp_path / "config.yaml"
        save_config({"a": 1}, config_path=str(path))
        data = yaml.safe_load(path.read_text())
        assert data == {"a": 1}

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "sub" / "dir" / "config.yaml"
        save_config({"x": "y"}, config_path=str(path))
        assert path.exists()

    def test_permissions_600(self, tmp_path):
        path = tmp_path / "config.yaml"
        save_config({}, config_path=str(path))
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600


class TestLoadConfig:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_config(str(tmp_path / "nope.yaml")) == {}

    def test_empty_file_returns_empty(self, tmp_path):
        path = tmp_path / "empty.yaml"
        path.write_text("")
        assert load_config(str(path)) == {}

    def test_loads_valid_yaml(self, tmp_path):
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump({"a": 1}))
        assert load_config(str(path)) == {"a": 1}

    def test_non_mapping_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("- item1\n- item2\n")
        with pytest.raises(ValueError, match="not a YAML mapping"):
            load_config(str(path))

    def test_malformed_yaml_raises(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(":::bad\n  yaml: [")
        with pytest.raises(ValueError, match="Malformed YAML"):
            load_config(str(path))
