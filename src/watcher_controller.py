from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

logger = logging.getLogger(__name__)


class WatcherController(QObject):
    change_detected = Signal()

    def __init__(
        self,
        sync_dir_name: str,
        *,
        debounce_ms: int = 2000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sync_dir_name = sync_dir_name
        self._sync_folder: Path | None = None
        self._watcher: QFileSystemWatcher | None = None
        self._paused = False
        self._last_metadata_mtime: float | None = None

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(debounce_ms)
        self._debounce.timeout.connect(self._on_debounce_fired)

    def update_sync_folder(self, folder: Path | None) -> None:
        self._teardown()
        self._sync_folder = folder
        if folder is not None:
            self._watcher = QFileSystemWatcher(self)
            self._watcher.fileChanged.connect(self._on_changed)
            self._watcher.directoryChanged.connect(self._on_changed)
            self._refresh_paths()

    def stop_debounce(self) -> None:
        self._debounce.stop()

    def pause(self) -> None:
        self._paused = True
        logger.debug("File watcher paused")

    def resume(self) -> None:
        self._record_mtime_baseline()
        self._refresh_paths()
        self._paused = False
        logger.debug("File watcher resumed")

    def record_mtime_baseline(self) -> None:
        self._record_mtime_baseline()

    def _teardown(self) -> None:
        if self._watcher is None:
            return
        watched = self._watcher.files() + self._watcher.directories()
        if watched:
            self._watcher.removePaths(watched)
        self._watcher.deleteLater()
        self._watcher = None

    def _refresh_paths(self) -> None:
        if self._watcher is None or self._sync_folder is None:
            return
        watched = self._watcher.files() + self._watcher.directories()
        if watched:
            self._watcher.removePaths(watched)
        if not self._sync_folder.exists():
            logger.debug("Watcher idle — sync folder missing")
            return
        paths = [str(self._sync_folder)]
        current = self._sync_folder / self._sync_dir_name
        if current.is_dir():
            paths.append(str(current))
            for sub in current.rglob("*"):
                if sub.is_dir():
                    paths.append(str(sub))
        self._watcher.addPaths(paths)
        logger.debug("Watching %d paths under %s", len(paths), self._sync_folder)

    def _record_mtime_baseline(self) -> None:
        if self._sync_folder is None:
            self._last_metadata_mtime = None
            return
        metadata = self._sync_folder / self._sync_dir_name / "metadata.json"
        try:
            self._last_metadata_mtime = metadata.stat().st_mtime if metadata.exists() else None
        except OSError:
            self._last_metadata_mtime = None

    def _on_changed(self, path: str) -> None:
        if self._paused:
            return
        logger.debug("Change: %s", path)
        self._debounce.start()

    def _on_debounce_fired(self) -> None:
        if self._sync_folder is not None and self._last_metadata_mtime is not None:
            metadata = self._sync_folder / self._sync_dir_name / "metadata.json"
            if metadata.exists():
                try:
                    if metadata.stat().st_mtime == self._last_metadata_mtime:
                        logger.debug("Watcher fired but metadata.json unchanged — skipping")
                        return
                except OSError:
                    pass
        self.change_detected.emit()
