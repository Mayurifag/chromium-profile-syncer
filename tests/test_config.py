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


def test_load_and_save_do_not_share_nested_mutable_config():
    original = {"nested": {"value": ["original"]}}
    config_module.save(original)
    original["nested"]["value"].append("changed outside config")
    loaded = config_module.load()
    loaded["nested"]["value"].append("changed snapshot")
    assert config_module.load() == {"nested": {"value": ["original"]}}


def test_remove_one_profile_preserves_other_profiles_and_shared_settings(tmp_path):
    profile = tmp_path / "Default"
    config_module.save(
        {
            "enabled_profiles": {"Browser": ["Default", "Profile 1"]},
            "enabled_browsers": {"Browser": True},
            "profiles_needing_restore": {"Browser": ["Default", "Profile 1"]},
            "profile_directions": {"Browser": {"Default": "push", "Profile 1": "both"}},
            "extension_observations": {str(profile.resolve()): ["extension"]},
            "sync_folder": "keep-shared-backup",
        }
    )
    config_module.remove_profile("Browser", "Default", profile)
    saved = config_module.load()
    assert saved["enabled_profiles"] == {"Browser": ["Profile 1"]}
    assert saved["enabled_browsers"] == {"Browser": True}
    assert saved["profiles_needing_restore"] == {"Browser": ["Profile 1"]}
    assert saved["profile_directions"] == {"Browser": {"Profile 1": "both"}}
    assert saved["extension_observations"] == {}
    assert saved["sync_folder"] == "keep-shared-backup"


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


def test_corrupt_config_is_not_silently_overwritten():
    config_module.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config_module.CONFIG_PATH.write_text("not valid json {{{", encoding="utf-8")
    with pytest.raises(ValueError):
        config_module.load()
    with pytest.raises(ValueError):
        config_module.set_autostart(False)
    assert config_module.CONFIG_PATH.read_text() == "not valid json {{{"


def test_set_sync_folder_persists_across_calls(tmp_path):
    folder = tmp_path / "my_sync"
    config_module.set_sync_folder(folder)
    # A second load should still return the same value
    assert config_module.get_sync_folder() == folder


def test_save_overwrites_existing_data():
    config_module.save({"a": 1})
    config_module.save({"b": 2})
    assert config_module.load() == {"b": 2}
