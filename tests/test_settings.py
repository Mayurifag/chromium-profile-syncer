from __future__ import annotations

import pytest

import src.config as config_module
from src.sync.sync_dir import SYNC_DIR_NAME

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

PySide6 = pytest.importorskip("PySide6")


def _get_or_create_app():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Redirect config paths to tmp_path for every test."""
    cfg_dir = tmp_path / "cfg"
    cfg_path = cfg_dir / "config.json"
    monkeypatch.setattr(config_module, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_module, "CONFIG_PATH", cfg_path)


@pytest.fixture(scope="module")
def qapp():
    return _get_or_create_app()


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


def test_settings_module_importable():
    from src.settings import SettingsDialog  # noqa: F401


def test_config_accessors_importable():
    from src.config import (  # noqa: F401
        get_autostart,
        get_enabled_browsers,
        get_enabled_profiles,
        get_profile_directions,
        set_autostart,
        set_enabled_browsers,
        set_enabled_profiles,
        set_profile_directions,
    )


# ---------------------------------------------------------------------------
# Config accessor round-trip tests
# ---------------------------------------------------------------------------


def test_enabled_browsers_default_empty():
    assert config_module.get_enabled_browsers() == {}


def test_enabled_browsers_roundtrip():
    browsers = {"Thorium": True, "Helium": False}
    config_module.set_enabled_browsers(browsers)
    assert config_module.get_enabled_browsers() == browsers


def test_enabled_profiles_default_empty():
    assert config_module.get_enabled_profiles() == {}


def test_enabled_profiles_roundtrip():
    profiles = {"Thorium": ["Default", "Profile 1"], "Helium": ["Default"]}
    config_module.set_enabled_profiles(profiles)
    assert config_module.get_enabled_profiles() == profiles


def test_autostart_default_true():
    assert config_module.get_autostart() is True


def test_autostart_roundtrip_false():
    config_module.set_autostart(False)
    assert config_module.get_autostart() is False


def test_autostart_roundtrip_true():
    config_module.set_autostart(True)
    assert config_module.get_autostart() is True


def test_profile_directions_default_empty():
    assert config_module.get_profile_directions() == {}


def test_profile_directions_roundtrip():
    directions = {
        "Thorium": {"Default": "push", "Profile 1": "pull"},
        "Chrome": {"Default": "both"},
    }
    config_module.set_profile_directions(directions)
    assert config_module.get_profile_directions() == directions


def test_config_accessors_coexist_with_sync_folder(tmp_path):
    """Ensure new accessors don't overwrite each other or sync_folder."""
    from pathlib import Path

    folder = tmp_path / "sync"
    config_module.set_sync_folder(folder)
    config_module.set_enabled_browsers({"Thorium": True})
    config_module.set_autostart(False)
    assert config_module.get_sync_folder() == Path(folder)
    assert config_module.get_enabled_browsers() == {"Thorium": True}
    assert config_module.get_autostart() is False


# ---------------------------------------------------------------------------
# Dialog construction tests (require QApplication)
# ---------------------------------------------------------------------------


class _MockBrowser:
    """Minimal browser stub that avoids real filesystem checks."""

    def __init__(self, name: str, profiles: list[str] | None = None):
        self._name = name
        self._profiles = profiles or ["Default", "Profile 1"]

    @property
    def name(self) -> str:
        return self._name

    def is_installed(self) -> bool:
        return True

    def is_running(self) -> bool:
        return False

    def discover_profiles(self):
        from pathlib import Path

        return [Path(p) for p in self._profiles]

    def get_profile_name(self, profile_path):
        """Mock implementation: just return the directory name."""
        return profile_path.name

    def profile_root(self):
        """Mock implementation: return None to skip Local State lookup."""
        return None


def test_settings_dialog_constructs(qapp):
    from src.settings import SettingsDialog

    dlg = SettingsDialog(browsers_list=[_MockBrowser("FakeBrowser")])
    assert dlg is not None
    dlg.close()


def test_settings_dialog_has_expected_attributes(qapp, tmp_path):
    from src.settings import SettingsDialog

    dlg = SettingsDialog(browsers_list=[_MockBrowser("Alpha"), _MockBrowser("Beta")])
    assert dlg._profile_states == {}
    assert dlg._autostart_select is not None
    dlg.close()


def test_settings_dialog_no_browsers(qapp):
    from src.settings import SettingsDialog

    dlg = SettingsDialog(browsers_list=[])
    assert dlg._profile_states == {}
    dlg.close()


def test_settings_dialog_profile_states_populated(qapp, tmp_path):
    from src.settings import SettingsDialog

    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    (sync_folder / SYNC_DIR_NAME).mkdir()
    config_module.set_sync_folder(sync_folder)

    mock = _MockBrowser("Chrome", profiles=["Default", "Profile 1", "Profile 2"])
    dlg = SettingsDialog(browsers_list=[mock])
    states = dlg._profile_states.get("Chrome", {})
    assert set(states.keys()) == {"Default", "Profile 1", "Profile 2"}
    dlg.close()


def test_rebuild_profiles_synced_profile_enabled(qapp, tmp_path):
    """Profile enabled in config gets state=True; others False."""
    from src.settings import SettingsDialog

    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    (sync_folder / SYNC_DIR_NAME).mkdir()

    # Save Default profile to config (simulating user enabling it)
    config_module.set_enabled_profiles({"Chrome": ["Default"]})

    mock = _MockBrowser("Chrome", profiles=["Default", "Profile 1"])
    dlg = SettingsDialog(browsers_list=[mock])
    dlg._rebuild_profiles(sync_folder)

    states = dlg._profile_states.get("Chrome", {})
    assert states["Default"] is True
    assert states["Profile 1"] is False
    dlg.close()


def test_settings_dialog_accept_saves_config(qapp, monkeypatch):
    """Changing autostart select saves config immediately."""
    from src.settings import SettingsDialog

    mock = _MockBrowser("Thorium", profiles=["Default"])
    dlg = SettingsDialog(browsers_list=[mock])

    # Simulate user changing autostart to "No" - should save immediately
    dlg._autostart_select.setCurrentIndex(1)  # 1 = No

    assert config_module.get_autostart() is False
    dlg.close()


def test_initial_upload_only_one_profile_enabled(qapp, tmp_path):
    """After initial upload of ONE profile, only that profile should show as enabled."""
    from src.settings import SettingsDialog, _sync_folder_has_profile
    from src.sync_engine import SyncEngine

    # Setup: clean state
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()

    # Clear config (simulating Clean button)
    config_module.set_enabled_profiles({})
    config_module.set_enabled_browsers({})

    # Create 3 local profiles
    profiles_dir = tmp_path / "profiles"
    default = profiles_dir / "Default"
    profile1 = profiles_dir / "Profile 1"
    profile2 = profiles_dir / "Profile 2"

    for p in [default, profile1, profile2]:
        p.mkdir(parents=True)
        (p / "Preferences").write_text("{}", encoding="utf-8")
        (p / "Bookmarks").write_text('{"roots":{}}', encoding="utf-8")

    # Simulate initial upload of ONLY Default profile
    import shutil

    from src.sync.sync_dir import merge_to_sync_dir
    engine = SyncEngine(sync_folder)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    engine.sync_browser_profile(default, work_dir, direction="push")
    current_dir = sync_folder / SYNC_DIR_NAME
    merge_to_sync_dir(work_dir, current_dir)
    (current_dir / "metadata.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(work_dir)

    # Save Default profile to config (this is what initial upload dialog does)
    config_module.set_enabled_profiles({"Chrome": ["Default"]})
    config_module.set_enabled_browsers({"Chrome": True})

    # Verify sync folder has data
    assert _sync_folder_has_profile(sync_folder)

    # Create dialog with all 3 profiles
    mock = _MockBrowser("Chrome", profiles=["Default", "Profile 1", "Profile 2"])
    dlg = SettingsDialog(browsers_list=[mock])
    dlg._rebuild_profiles(sync_folder)

    # Verify only Default shows as enabled
    states = dlg._profile_states.get("Chrome", {})
    assert states["Default"] is True, "Default profile should be enabled after upload"
    assert states["Profile 1"] is False, "Profile 1 should NOT be enabled"
    assert states["Profile 2"] is False, "Profile 2 should NOT be enabled"

    dlg.close()


def test_sync_folder_has_profile(qapp, tmp_path):
    """_sync_folder_has_profile detects current/ directory."""
    from src.settings import _sync_folder_has_profile

    sync_folder = tmp_path / "sync"
    assert not _sync_folder_has_profile(sync_folder)

    sync_folder.mkdir()
    assert not _sync_folder_has_profile(sync_folder)

    (sync_folder / SYNC_DIR_NAME).mkdir()
    assert _sync_folder_has_profile(sync_folder)


def test_clean_then_upload_clears_old_profiles(qapp, tmp_path):
    """After Clean, only the newly uploaded profile should show as enabled."""
    from src.settings import SettingsDialog, _sync_folder_has_profile
    from src.sync_engine import SyncEngine

    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()

    # Setup: simulate previously synced state
    profiles_dir = tmp_path / "profiles"
    default = profiles_dir / "Default"
    profile1 = profiles_dir / "Profile 1"

    for p in [default, profile1]:
        p.mkdir(parents=True)
        (p / "Preferences").write_text("{}", encoding="utf-8")

    # Sync Default initially to current/
    import shutil

    from src.sync.sync_dir import merge_to_sync_dir
    engine = SyncEngine(sync_folder)
    work_dir = tmp_path / "work1"
    work_dir.mkdir()
    engine.sync_browser_profile(default, work_dir, direction="push")
    current_dir = sync_folder / SYNC_DIR_NAME
    merge_to_sync_dir(work_dir, current_dir)
    (current_dir / "metadata.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(work_dir)

    # Save to config
    config_module.set_enabled_profiles({"Chrome": ["Default", "Profile 1"]})
    config_module.set_enabled_browsers({"Chrome": True})

    assert _sync_folder_has_profile(sync_folder)

    # Simulate Clean button: delete sync data and clear config
    shutil.rmtree(sync_folder / SYNC_DIR_NAME)
    config_module.set_enabled_profiles({})
    config_module.set_enabled_browsers({})

    # Verify config is cleared
    assert config_module.get_enabled_profiles() == {}
    assert not _sync_folder_has_profile(sync_folder)

    # Now upload only Default profile (simulating initial upload after clean)
    work_dir2 = tmp_path / "work2"
    work_dir2.mkdir()
    engine.sync_browser_profile(default, work_dir2, direction="push")
    current_dir2 = sync_folder / SYNC_DIR_NAME
    merge_to_sync_dir(work_dir2, current_dir2)
    (current_dir2 / "metadata.json").write_text("{}", encoding="utf-8")
    shutil.rmtree(work_dir2)

    # Save Default to config (simulating initial upload dialog)
    config_module.set_enabled_profiles({"Chrome": ["Default"]})
    config_module.set_enabled_browsers({"Chrome": True})

    assert _sync_folder_has_profile(sync_folder)

    # Build UI and verify only Default shows as enabled
    mock = _MockBrowser("Chrome", profiles=["Default", "Profile 1"])
    dlg = SettingsDialog(browsers_list=[mock])
    dlg._rebuild_profiles(sync_folder)

    states = dlg._profile_states.get("Chrome", {})
    assert states["Default"] is True, "Default should be enabled (just uploaded)"
    assert states["Profile 1"] is False, "Profile 1 should NOT be enabled (was cleaned)"

    dlg.close()


def test_load_current_settings_empty_folder_triggers_initial_upload(qapp, tmp_path):
    """Startup with a configured but empty sync folder must show the initial upload dialog,
    not skip straight to rebuild_profiles."""
    from unittest.mock import patch

    from src.settings import SettingsDialog

    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    config_module.set_sync_folder(sync_folder)

    mock = _MockBrowser("Chrome", profiles=["Default"])
    picked = []

    def _fake_pick(self):
        picked.append(True)
        return None  # simulate user cancelling — avoids any blocking dialog

    with patch.object(SettingsDialog, "_pick_initial_upload_profile", _fake_pick):
        dlg = SettingsDialog(browsers_list=[mock])

    assert picked, "_pick_initial_upload_profile was not called for empty sync folder"
    # _rebuild_profiles was not called, so _profile_states stays empty.
    assert "Chrome" not in dlg._profile_states
    dlg.close()


def test_load_current_settings_with_data_calls_rebuild_not_upload(qapp, tmp_path):
    """Startup with a configured folder that has data must call rebuild_profiles,
    not the initial upload dialog."""
    from src.settings import SettingsDialog

    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    (sync_folder / SYNC_DIR_NAME).mkdir()
    config_module.set_sync_folder(sync_folder)

    mock = _MockBrowser("Chrome", profiles=["Default"])
    dlg = SettingsDialog(browsers_list=[mock])
    # _rebuild_profiles populates _profile_states; initial-upload path does not.
    assert "Chrome" in dlg._profile_states, "_rebuild_profiles was not called for folder with data"
    dlg.close()
