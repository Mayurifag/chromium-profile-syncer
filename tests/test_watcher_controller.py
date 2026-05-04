from __future__ import annotations

from unittest.mock import MagicMock

import pytest

PySide6 = pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    return app


def _make_controller(parent=None, debounce_ms=2000):
    from src.watcher_controller import WatcherController
    return WatcherController("CPS_Sync", debounce_ms=debounce_ms, parent=parent)


def test_update_sync_folder_creates_watcher(qapp, tmp_path):
    ctl = _make_controller()
    ctl.update_sync_folder(tmp_path)
    assert ctl._watcher is not None


def test_update_sync_folder_none_tears_down(qapp, tmp_path):
    ctl = _make_controller()
    ctl.update_sync_folder(tmp_path)
    assert ctl._watcher is not None
    ctl.update_sync_folder(None)
    assert ctl._watcher is None


def test_update_sync_folder_replaces_watcher(qapp, tmp_path):
    ctl = _make_controller()
    ctl.update_sync_folder(tmp_path)
    first = ctl._watcher
    ctl.update_sync_folder(tmp_path)
    assert ctl._watcher is not first


def test_pause_blocks_change_signal(qapp, tmp_path):
    ctl = _make_controller(debounce_ms=0)
    ctl.update_sync_folder(tmp_path)
    ctl.pause()

    handler = MagicMock()
    ctl.change_detected.connect(handler)

    ctl._on_changed(str(tmp_path))
    assert not ctl._debounce.isActive()


def test_resume_clears_pause(qapp, tmp_path):
    ctl = _make_controller(debounce_ms=0)
    ctl.update_sync_folder(tmp_path)
    ctl.pause()
    assert ctl._paused

    ctl.resume()
    assert not ctl._paused


def test_debounce_resets_on_repeated_events(qapp, tmp_path):
    from PySide6.QtCore import QCoreApplication

    ctl = _make_controller(debounce_ms=0)
    ctl.update_sync_folder(tmp_path)

    handler = MagicMock()
    ctl.change_detected.connect(handler)

    ctl._on_changed(str(tmp_path))
    ctl._on_changed(str(tmp_path))
    assert ctl._debounce.isActive()

    QCoreApplication.processEvents()
    handler.assert_called_once()


def test_change_skipped_when_metadata_mtime_unchanged(qapp, tmp_path):
    from PySide6.QtCore import QCoreApplication

    sync_dir = tmp_path / "CPS_Sync"
    sync_dir.mkdir()
    metadata = sync_dir / "metadata.json"
    metadata.write_text("{}")

    ctl = _make_controller(debounce_ms=0)
    ctl.update_sync_folder(tmp_path)
    ctl.record_mtime_baseline()

    handler = MagicMock()
    ctl.change_detected.connect(handler)

    ctl._on_changed(str(tmp_path))
    QCoreApplication.processEvents()
    handler.assert_not_called()


def test_stop_debounce(qapp, tmp_path):
    ctl = _make_controller(debounce_ms=2000)
    ctl.update_sync_folder(tmp_path)
    ctl._on_changed(str(tmp_path))
    assert ctl._debounce.isActive()
    ctl.stop_debounce()
    assert not ctl._debounce.isActive()
