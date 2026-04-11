from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

PySide6 = pytest.importorskip("PySide6")


def _get_or_create_app():
    """Return the existing QApplication or create one for testing."""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


# ---------------------------------------------------------------------------
# Importability
# ---------------------------------------------------------------------------


def test_sync_worker_importable():
    from src.tray import SyncWorker  # noqa: F401


def test_tray_app_importable():
    from src.tray import TrayApp  # noqa: F401


def test_config_module_importable():
    from src import config  # noqa: F401

    assert hasattr(config, "load")
    assert hasattr(config, "save")
    assert hasattr(config, "get_sync_folder")
    assert hasattr(config, "set_sync_folder")


# ---------------------------------------------------------------------------
# QApplication-dependent tests
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    """Session-scoped QApplication fixture."""
    return _get_or_create_app()


def test_make_icon_idle(qapp):
    from PySide6.QtGui import QIcon

    from src.tray import TrayApp

    icon = TrayApp._make_icon("idle")
    assert isinstance(icon, QIcon)
    assert not icon.isNull()


@pytest.mark.parametrize("state", ["idle", "syncing", "waiting", "error"])
def test_make_icon_all_states(qapp, state):
    from PySide6.QtGui import QIcon

    from src.tray import TrayApp

    icon = TrayApp._make_icon(state)
    assert isinstance(icon, QIcon)
    assert not icon.isNull()


def test_make_icon_unknown_state_falls_back_to_idle(qapp):
    from PySide6.QtGui import QIcon

    from src.tray import TrayApp

    icon = TrayApp._make_icon("nonexistent_state")
    assert isinstance(icon, QIcon)
    assert not icon.isNull()


# ---------------------------------------------------------------------------
# TrayApp instance helpers
# ---------------------------------------------------------------------------


def _make_tray(qapp, tmp_path):
    """Create a TrayApp with a minimal mock engine and real config module."""
    from src import config
    from src.sync_engine import SyncEngine
    from src.tray import TrayApp

    engine = SyncEngine(tmp_path)
    tray = TrayApp(engine, config)
    return tray


# ---------------------------------------------------------------------------
# _teardown_watcher tests
# ---------------------------------------------------------------------------


def test_teardown_watcher_removes_paths(qapp, tmp_path):
    """_teardown_watcher calls removePaths and sets _watcher to None."""
    tray = _make_tray(qapp, tmp_path)

    mock_watcher = MagicMock()
    mock_watcher.files.return_value = [str(tmp_path)]
    mock_watcher.directories.return_value = []
    tray._watcher = mock_watcher

    tray._teardown_watcher()

    mock_watcher.removePaths.assert_called_once_with([str(tmp_path)])
    assert tray._watcher is None


def test_teardown_watcher_noop_when_none(qapp, tmp_path):
    """_teardown_watcher is safe to call when watcher is already None."""
    tray = _make_tray(qapp, tmp_path)
    tray._watcher = None
    tray._teardown_watcher()  # must not raise
    assert tray._watcher is None


def test_teardown_watcher_empty_paths(qapp, tmp_path):
    """_teardown_watcher skips removePaths when no paths are watched."""
    tray = _make_tray(qapp, tmp_path)

    mock_watcher = MagicMock()
    mock_watcher.files.return_value = []
    mock_watcher.directories.return_value = []
    tray._watcher = mock_watcher

    tray._teardown_watcher()

    mock_watcher.removePaths.assert_not_called()
    assert tray._watcher is None


# ---------------------------------------------------------------------------
# open_settings tests
# ---------------------------------------------------------------------------


def test_open_settings_creates_dialog(qapp, tmp_path):
    """open_settings instantiates SettingsDialog and calls exec()."""
    tray = _make_tray(qapp, tmp_path)

    mock_dialog = MagicMock()
    mock_dialog.settings_saved = MagicMock()
    mock_dialog.settings_saved.connect = MagicMock()

    with patch("src.tray.SettingsDialog", return_value=mock_dialog) as mock_cls:
        tray.open_settings()

    mock_cls.assert_called_once_with(parent=None)
    mock_dialog.settings_saved.connect.assert_called_once_with(tray._on_settings_saved)
    mock_dialog.exec.assert_called_once()


# ---------------------------------------------------------------------------
# _on_settings_saved tests
# ---------------------------------------------------------------------------


def test_on_settings_saved_rebuilds_engine_and_watcher(qapp, tmp_path):
    """_on_settings_saved replaces engine and watcher when folder is set."""
    from src import config as real_config
    from src.tray import TrayApp

    engine_mock = MagicMock()
    tray = _make_tray(qapp, tmp_path)
    old_engine = tray._engine

    mock_config = MagicMock()
    mock_config.get_sync_folder.return_value = tmp_path
    tray._config = mock_config

    with patch("src.tray.SyncEngine") as mock_engine_cls:
        mock_new_engine = MagicMock()
        mock_engine_cls.return_value = mock_new_engine
        tray._on_settings_saved()

    mock_engine_cls.assert_called_once_with(tmp_path)
    assert tray._engine is mock_new_engine


def test_on_settings_saved_no_folder_keeps_engine(qapp, tmp_path):
    """_on_settings_saved keeps existing engine when no sync folder is configured."""
    tray = _make_tray(qapp, tmp_path)
    old_engine = tray._engine

    mock_config = MagicMock()
    mock_config.get_sync_folder.return_value = None
    tray._config = mock_config

    with patch("src.tray.SyncEngine") as mock_engine_cls:
        tray._on_settings_saved()

    mock_engine_cls.assert_not_called()
    assert tray._engine is old_engine
