from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock, patch

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


@pytest.mark.parametrize("failed", [False, True])
def test_sync_worker_keeps_native_thread_finished_signal(qapp, failed):
    from PySide6.QtTest import QSignalSpy

    from src.sync_worker import SyncWorker

    engine = MagicMock()
    engine.sync_all.return_value = {"is_first_sync": False, "skipped_running": []}
    if failed:
        engine.sync_all.side_effect = OSError("test failure")
    worker = SyncWorker(engine)
    stopped = QSignalSpy(worker.finished)
    completed = QSignalSpy(worker.completed)
    errors = QSignalSpy(worker.error)
    worker.start()
    assert worker.wait(5000)
    qapp.processEvents()
    assert stopped.count() == 1
    assert completed.count() == (0 if failed else 1)
    assert errors.count() == (1 if failed else 0)


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

    mock_cls.assert_called_once_with(browser_monitor=ANY, parent=None)
    mock_dialog.settings_saved.connect.assert_called_once_with(tray._on_settings_saved)
    mock_dialog.exec.assert_called_once()


# ---------------------------------------------------------------------------
# _on_settings_saved tests
# ---------------------------------------------------------------------------


def test_on_settings_saved_rebuilds_engine_and_watcher(qapp, tmp_path):
    """_on_settings_saved replaces engine and watcher when folder is set."""

    tray = _make_tray(qapp, tmp_path)

    mock_config = MagicMock()
    mock_config.get_sync_folder.return_value = tmp_path
    mock_config.get_sync_interval.return_value = 15
    mock_config.get_enabled_profiles.return_value = {}
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


# ---------------------------------------------------------------------------
# Permissions check tests
# ---------------------------------------------------------------------------


def test_check_sync_folder_permissions_accessible(tmp_path):
    """_check_sync_folder_permissions returns True for a readable/writable path."""
    from src.tray import TrayApp

    assert TrayApp._check_sync_folder_permissions(tmp_path) is True


def test_check_sync_folder_permissions_missing():
    """_check_sync_folder_permissions returns False for a nonexistent path."""
    from src.tray import TrayApp

    assert TrayApp._check_sync_folder_permissions(Path("/nonexistent/path")) is False


def test_trigger_sync_warns_on_bad_permissions(qapp, tmp_path):
    """_trigger_sync shows a warning and skips SyncWorker when permissions fail."""
    tray = _make_tray(qapp, tmp_path)

    mock_config = MagicMock()
    mock_config.get_sync_folder.return_value = tmp_path
    mock_config.get_sync_interval.return_value = 30
    tray._config = mock_config

    with (
        patch("src.tray.notify") as mock_notify,
        patch("src.tray.TrayApp._check_sync_folder_permissions", return_value=False),
    ):
        tray._trigger_sync()

    assert tray._worker is None
    mock_notify.assert_called_once()


# ---------------------------------------------------------------------------
# rclone missing warning
# ---------------------------------------------------------------------------


def test_quit_waits_for_active_sync_worker(qapp, tmp_path):
    tray = _make_tray(qapp, tmp_path)
    tray._worker = MagicMock()
    tray._worker.isRunning.return_value = True
    with (
        patch("src.tray.QApplication.quit") as quit_app,
        patch("src.tray.QTimer.singleShot") as retry,
    ):
        tray.request_quit()
        quit_app.assert_not_called()
        retry.assert_called_once_with(250, tray.request_quit)
        tray._worker.isRunning.return_value = False
        tray.request_quit()
        quit_app.assert_called_once()
