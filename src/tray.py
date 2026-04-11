from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QFileSystemWatcher, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from src import autostart
from src.browsers import ALL_BROWSERS
from src.settings import SettingsDialog
from src.sync_engine import SyncEngine

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_ICON_COLORS: dict[str, str] = {
    "idle": "#808080",
    "syncing": "#4A90D9",
    "waiting": "#E8A317",
    "error": "#D94A4A",
}


class SyncWorker(QThread):
    started = Signal()  # type: ignore[assignment]
    finished = Signal(str)
    error = Signal(str)

    def __init__(self, engine: SyncEngine) -> None:
        super().__init__()
        self._engine = engine

    def run(self) -> None:
        self.started.emit()
        logger.info("SyncWorker: sync started")
        try:
            self._engine.sync_all()
            ts = datetime.now(tz=timezone.utc).isoformat()
            logger.info("SyncWorker: sync finished at %s", ts)
            self.finished.emit(ts)
        except Exception as exc:
            logger.exception("SyncWorker: sync error")
            self.error.emit(str(exc))


class TrayApp(QSystemTrayIcon):
    def __init__(
        self,
        engine: SyncEngine,
        config_module,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._engine = engine
        self._config = config_module
        self._worker: SyncWorker | None = None
        self._debounce_pending: bool = False
        self._watcher: QFileSystemWatcher | None = None

        self.setIcon(self._make_icon("idle"))
        self.setToolTip("Chromium Profile Syncer")

        self._menu = QMenu()
        self._action_sync = self._menu.addAction("Sync Now")
        self._action_sync.triggered.connect(self._trigger_sync)

        self._action_settings = self._menu.addAction("Settings")
        self._action_settings.triggered.connect(self.open_settings)

        self._menu.addSeparator()

        self._action_status = self._menu.addAction("Idle")
        self._action_status.setEnabled(False)

        self._menu.addSeparator()

        self._action_quit = self._menu.addAction("Quit")
        self._action_quit.triggered.connect(QApplication.quit)

        self.setContextMenu(self._menu)

        self._setup_timer()

        sync_folder = self._config.get_sync_folder()
        if sync_folder is not None:
            self._setup_watcher(sync_folder)

        logger.debug("TrayApp initialized")

    # ------------------------------------------------------------------
    # Icon generation
    # ------------------------------------------------------------------

    @staticmethod
    def _make_icon(state: str) -> QIcon:
        """Draw a 22x22 filled circle for the given state."""
        color_hex = _ICON_COLORS.get(state, _ICON_COLORS["idle"])
        pixmap = QPixmap(22, 22)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(color_hex))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(1, 1, 20, 20)
        painter.end()
        return QIcon(pixmap)

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    def _setup_timer(self) -> None:
        self._timer = QTimer(self)
        self._timer.setInterval(15 * 60 * 1000)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()
        logger.debug("Periodic sync timer armed (15 min)")

    def _on_timer(self) -> None:
        logger.info("Periodic timer fired — triggering sync")
        self._trigger_sync()

    # ------------------------------------------------------------------
    # File watcher
    # ------------------------------------------------------------------

    def _setup_watcher(self, sync_folder: Path) -> None:
        self._watcher = QFileSystemWatcher(self)
        paths_to_watch: list[str] = []

        # Always watch the sync folder directory itself
        if sync_folder.exists():
            paths_to_watch.append(str(sync_folder))

        # Watch metadata.json only if it exists (addPath silently ignores missing paths)
        meta = sync_folder / "metadata.json"
        if meta.exists():
            paths_to_watch.append(str(meta))

        if paths_to_watch:
            self._watcher.addPaths(paths_to_watch)
            logger.debug("Watching paths: %s", paths_to_watch)
        else:
            logger.debug("Sync folder %s does not exist yet — watcher idle", sync_folder)

        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_dir_changed)

    def _on_file_changed(self, path: str) -> None:
        logger.debug("File changed: %s", path)
        self._schedule_debounced_sync()

    def _on_dir_changed(self, path: str) -> None:
        logger.debug("Directory changed: %s", path)
        self._schedule_debounced_sync()

    def _schedule_debounced_sync(self) -> None:
        if self._debounce_pending:
            logger.debug("Debounce already pending — skipping")
            return
        self._debounce_pending = True
        QTimer.singleShot(2000, self._on_debounce_fired)
        logger.debug("Debounce timer started (2s)")

    def _on_debounce_fired(self) -> None:
        self._debounce_pending = False
        logger.info("Debounce fired — triggering sync")
        self._trigger_sync()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def open_settings(self) -> None:
        """Open the SettingsDialog and connect its saved signal."""
        dialog = SettingsDialog(parent=None)
        dialog.settings_saved.connect(self._on_settings_saved)
        dialog.exec()

    def _on_settings_saved(self) -> None:
        """Rebuild SyncEngine and file watcher after settings are saved."""
        sync_folder = self._config.get_sync_folder()
        if sync_folder is None:
            logger.warning("Settings saved but no sync folder chosen — keeping existing engine")
            autostart.apply(self._config.get_autostart())
            return

        self._engine = SyncEngine(sync_folder)
        logger.info("Settings saved — engine rebuilt with folder %s", sync_folder)

        self._teardown_watcher()
        self._setup_watcher(sync_folder)
        logger.info("File watcher rebuilt for %s", sync_folder)
        autostart.apply(self._config.get_autostart())
        logger.info("Autostart registration updated")

    def _teardown_watcher(self) -> None:
        """Stop and discard the current file watcher."""
        if self._watcher is not None:
            watched = self._watcher.files() + self._watcher.directories()
            if watched:
                self._watcher.removePaths(watched)
                logger.debug("Removed watched paths: %s", watched)
            self._watcher = None
            logger.debug("File watcher torn down")

    # ------------------------------------------------------------------
    # Sync orchestration
    # ------------------------------------------------------------------

    def _trigger_sync(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            logger.info("Sync already in progress — skipping")
            return

        sync_folder = self._config.get_sync_folder()
        if sync_folder is None:
            logger.warning("No sync folder configured — cannot sync")
            self.showMessage(
                "Chromium Profile Syncer",
                "No sync folder configured. Open Settings to choose one.",
                QSystemTrayIcon.MessageIcon.Warning,
            )
            return

        logger.info("Starting sync worker")
        self._worker = SyncWorker(self._engine)
        self._worker.started.connect(self._on_sync_started)
        self._worker.finished.connect(self._on_sync_finished)
        self._worker.error.connect(self._on_sync_error)
        self._worker.start()

    def _on_sync_started(self) -> None:
        logger.debug("Sync started signal received")
        self.setIcon(self._make_icon("syncing"))
        self._action_status.setText("Syncing...")

    def _on_sync_finished(self, last_sync: str) -> None:
        logger.info("Sync finished at %s", last_sync)
        # Check if any browser is currently running — if so, use waiting state
        any_running = any(b.is_running() for b in ALL_BROWSERS)
        state = "waiting" if any_running else "idle"
        self.setIcon(self._make_icon(state))
        self._action_status.setText(f"Last sync: {last_sync}")

    def _on_sync_error(self, msg: str) -> None:
        logger.error("Sync error: %s", msg)
        self.setIcon(self._make_icon("error"))
        self._action_status.setText(f"Error: {msg}")
