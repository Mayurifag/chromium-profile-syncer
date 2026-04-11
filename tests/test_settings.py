from __future__ import annotations

import pytest

import src.config as config_module

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
        get_enabled_data_types,
        get_enabled_profiles,
        get_profile_directions,
        set_autostart,
        set_enabled_browsers,
        set_enabled_data_types,
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


def test_enabled_data_types_default_all_true():
    dt = config_module.get_enabled_data_types()
    for key in ("extensions", "bookmarks", "custom_dictionary", "local_storage", "indexeddb"):
        assert dt[key] is True


def test_enabled_data_types_roundtrip():
    data_types = {
        "extensions": True,
        "bookmarks": False,
        "custom_dictionary": True,
        "local_storage": False,
        "indexeddb": True,
    }
    config_module.set_enabled_data_types(data_types)
    assert config_module.get_enabled_data_types() == data_types


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
    directions = {"Thorium": {"Default": "push", "Profile 1": "pull"}, "Chrome": {"Default": "both"}}
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

    def discover_profiles(self):
        from pathlib import Path

        return [Path(p) for p in self._profiles]


def test_settings_dialog_constructs(qapp):
    from src.settings import SettingsDialog

    dlg = SettingsDialog(browsers_list=[_MockBrowser("FakeBrowser")])
    assert dlg is not None
    dlg.close()


def test_settings_dialog_has_expected_attributes(qapp):
    from src.settings import SettingsDialog

    dlg = SettingsDialog(browsers_list=[_MockBrowser("Alpha"), _MockBrowser("Beta")])
    assert "Alpha" in dlg._profile_checks
    assert "Beta" in dlg._profile_checks
    assert "Alpha" in dlg._profile_directions
    assert "Beta" in dlg._profile_directions
    assert len(dlg._data_type_checks) == 5
    assert dlg._autostart_check is not None
    dlg.close()


def test_settings_dialog_no_browsers(qapp):
    from src.settings import SettingsDialog

    dlg = SettingsDialog(browsers_list=[])
    assert dlg._profile_checks == {}
    dlg.close()


def test_settings_dialog_profile_checks_populated(qapp):
    from src.settings import SettingsDialog

    mock = _MockBrowser("Chrome", profiles=["Default", "Profile 1", "Profile 2"])
    dlg = SettingsDialog(browsers_list=[mock])
    profiles = dlg._profile_checks.get("Chrome", {})
    assert set(profiles.keys()) == {"Default", "Profile 1", "Profile 2"}
    dlg.close()


def test_settings_dialog_accept_saves_config(qapp, monkeypatch):
    """Accepting the dialog persists values to config."""
    from src.settings import SettingsDialog

    mock = _MockBrowser("Thorium", profiles=["Default"])
    dlg = SettingsDialog(browsers_list=[mock])

    # Simulate user toggling autostart off
    dlg._autostart_check.setChecked(False)
    dlg._on_accept()

    assert config_module.get_autostart() is False
    dlg.close()
