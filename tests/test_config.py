from __future__ import annotations

from pathlib import Path

import pytest

import src.config as config_module


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect config paths to tmp_path for every test."""
    cfg_dir = tmp_path / "cfg"
    cfg_path = cfg_dir / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_path)
    return cfg_path


def test_load_returns_empty_when_missing():
    assert config_module.load() == {}


def test_save_creates_file_and_load_reads_it_back(tmp_path):
    data = {"key": "value", "num": 42}
    config_module.save(data)
    assert config_module.load() == data


def test_save_creates_parent_directory():
    config_module.save({"x": 1})
    assert config_module.CONFIG_PATH.exists()
    assert config_module.CONFIG_DIR.is_dir()


def test_get_sync_folder_returns_none_when_not_set():
    assert config_module.get_sync_folder() is None


def test_set_and_get_sync_folder(tmp_path):
    folder = tmp_path / "sync"
    config_module.set_sync_folder(folder)
    result = config_module.get_sync_folder()
    assert result == folder
    assert isinstance(result, Path)


def test_load_returns_empty_on_corrupt_json():
    config_module.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_module.CONFIG_PATH.write_text("not valid json {{{", encoding="utf-8")
    assert config_module.load() == {}


def test_set_sync_folder_persists_across_calls(tmp_path):
    folder = tmp_path / "my_sync"
    config_module.set_sync_folder(folder)
    # A second load should still return the same value
    assert config_module.get_sync_folder() == folder


def test_save_overwrites_existing_data():
    config_module.save({"a": 1})
    config_module.save({"b": 2})
    assert config_module.load() == {"b": 2}
