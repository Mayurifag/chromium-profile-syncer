from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QFileSystemWatcher, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from src import autostart
from src.browsers import ALL_BROWSERS
from src.dracula import ICON_COLORS
from src.settings import SettingsDialog
from src.sync_engine import SyncEngine, find_rclone
from src.sync_progress import SyncProgressDialog

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SyncWorker(QThread):
    started = Signal()  # type: ignore[assignment]
    finished = Signal(str, bool)  # timestamp, is_first_sync
    error = Signal(str)
    progress = Signal(str)  # type: ignore[assignment]
    profile_progress = Signal(str, str, str, int, float)  # browser, profile, dir, count, elapsed

    def __init__(self, engine: SyncEngine) -> None:
        super().__init__()
        self._engine = engine
        self._current_profile: tuple[str, str, str] | None = None  # (browser, profile, direction)
        self._profile_count = 0
        self._profile_start = 0.0

    def run(self) -> None:
        self.started.emit()
        logger.info("SyncWorker: sync started")
        try:
            def _progress_handler(desc: str) -> None:
                self.progress.emit(desc)
                if "/" in desc:
                    parts = desc.split("/", 1)
                    browser, profile = parts[0], parts[1]

                    if self._current_profile is None or \
                       self._current_profile[0] != browser or \
                       self._current_profile[1] != profile:
                        from src import config as _config
                        directions = _config.get_profile_directions()
                        direction = directions.get(browser, {}).get(profile, "both")
                        direction_label = {
                            "push": "TO",
                            "pull": "FROM",
                            "both": "FROM/TO",
                        }.get(direction, direction)

                        self._current_profile = (browser, profile, direction_label)
                        self._profile_count = 0
                        self._profile_start = datetime.now().timestamp()

                    self._profile_count += 1
                    elapsed = datetime.now().timestamp() - self._profile_start

                    last_emit = getattr(self, "_last_emit", 0)
                    if self._profile_count % 10 == 0 or elapsed - last_emit > 1.0:
                        self.profile_progress.emit(
                            browser,
                            profile,
                            self._current_profile[2],
                            self._profile_count,
                            elapsed,
                        )
                        self._last_emit = elapsed

            self._engine._progress_cb = _progress_handler
            result = self._engine.sync_all()
            ts = datetime.now(tz=UTC).isoformat()
            is_first_sync = result.get("is_first_sync", False)
            logger.info("SyncWorker: sync finished at %s", ts)
            self.finished.emit(ts, is_first_sync)
        except Exception as exc:
            logger.exception("SyncWorker: sync error")
            self.error.emit(str(exc))
        finally:
            self._engine._progress_cb = None
            self._current_profile = None


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
        self._debounce_timer = QTimer(self)
        self._debounce_timer.setSingleShot(True)
        self._debounce_timer.setInterval(2000)
        self._debounce_timer.timeout.connect(self._on_debounce_fired)
        self._watcher: QFileSystemWatcher | None = None
        self._watcher_paused: bool = False
        self._settings_dialog: SettingsDialog | None = None
        self._progress_dialog: SyncProgressDialog | None = None
        self._next_sync_time: float = 0.0
        self._countdown_timer: QTimer | None = None

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
        self._setup_countdown_timer()

        sync_folder = self._config.get_sync_folder()
        if sync_folder is not None:
            self._setup_watcher(sync_folder)

        logger.debug("TrayApp initialized")

        if find_rclone() is None:
            QTimer.singleShot(500, self._warn_rclone_missing)

    # ------------------------------------------------------------------
    # Icon generation
    # ------------------------------------------------------------------

    @staticmethod
    def _make_icon(state: str) -> QIcon:
        color_hex = ICON_COLORS.get(state, ICON_COLORS["idle"])
        pixmap = QPixmap(22, 22)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setBrush(QColor(color_hex))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(1, 1, 20, 20)
        painter.end()
        return QIcon(pixmap)

    def _warn_rclone_missing(self) -> None:
        self.showMessage(
            "rclone not found",
            "rclone is required for sync. Install via Homebrew: brew install rclone",
            QSystemTrayIcon.MessageIcon.Warning,
        )

    # ------------------------------------------------------------------
    # Timer
    # ------------------------------------------------------------------

    def _setup_timer(self) -> None:
        interval_minutes = self._config.get_sync_interval()
        self._timer = QTimer(self)
        self._timer.setInterval(interval_minutes * 60 * 1000)
        self._timer.timeout.connect(self._on_timer)
        self._timer.start()
        self._next_sync_time = time.time() + (interval_minutes * 60)
        logger.debug("Periodic sync timer armed (%d min)", interval_minutes)

    def _setup_countdown_timer(self) -> None:
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(1000)
        self._countdown_timer.timeout.connect(self._update_countdown)
        self._countdown_timer.start()

    def _update_countdown(self) -> None:
        if self._settings_dialog is not None:
            remaining = int(self._next_sync_time - time.time())
            self._settings_dialog.update_next_sync_time(max(0, remaining))

    def _on_timer(self) -> None:
        logger.info("Periodic timer fired — triggering sync")
        self._trigger_sync()
        interval_minutes = self._config.get_sync_interval()
        self._next_sync_time = time.time() + (interval_minutes * 60)

    # ------------------------------------------------------------------
    # File watcher
    # ------------------------------------------------------------------

    def _setup_watcher(self, sync_folder: Path) -> None:
        self._watcher = QFileSystemWatcher(self)
        paths_to_watch: list[str] = []

        if sync_folder.exists():
            paths_to_watch.append(str(sync_folder))

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
        if self._watcher_paused:
            logger.debug("File changed (ignored, watcher paused): %s", path)
            return
        logger.debug("File changed: %s", path)
        self._schedule_debounced_sync()

    def _on_dir_changed(self, path: str) -> None:
        if self._watcher_paused:
            logger.debug("Directory changed (ignored, watcher paused): %s", path)
            return
        logger.debug("Directory changed: %s", path)
        self._schedule_debounced_sync()

    def _schedule_debounced_sync(self) -> None:
        self._debounce_timer.start()
        logger.debug("Debounce timer reset (2s)")

    def _on_debounce_fired(self) -> None:
        logger.info("Debounce fired — triggering sync")
        self._trigger_sync()

    def _resume_watcher(self) -> None:
        self._watcher_paused = False
        logger.debug("File watcher resumed")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def open_settings(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.show()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return

        dialog = SettingsDialog(parent=None)
        dialog.settings_saved.connect(self._on_settings_saved)
        dialog.sync_requested.connect(self._trigger_sync)
        dialog.sync_interval_changed.connect(self._on_sync_interval_changed)
        dialog.finished.connect(lambda: self._on_settings_closed())

        self._settings_dialog = dialog

        if find_rclone() is None:
            self._warn_rclone_missing()

        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

        dialog.exec()

    def _on_settings_closed(self) -> None:
        self._settings_dialog = None

    def _on_progress_dialog_closed(self) -> None:
        self._progress_dialog = None

    def _on_sync_interval_changed(self, minutes: int) -> None:
        if self._timer is not None:
            self._timer.stop()
            self._timer.setInterval(minutes * 60 * 1000)
            self._timer.start()
            self._next_sync_time = time.time() + (minutes * 60)
            logger.info("Sync interval updated to %d minutes", minutes)

    def _on_settings_saved(self) -> None:
        sync_folder = self._config.get_sync_folder()
        if sync_folder is None:
            logger.warning("Settings saved but no sync folder chosen — keeping existing engine")
            return

        self._engine = SyncEngine(sync_folder)
        logger.info("Settings saved — engine rebuilt with folder %s", sync_folder)

        self._teardown_watcher()
        self._setup_watcher(sync_folder)
        logger.info("File watcher rebuilt for %s", sync_folder)

        has_profiles = any(
            profiles
            for profiles in self._config.get_enabled_profiles().values()
        )
        autostart.apply(has_profiles)
        logger.info("Autostart %s", "enabled" if has_profiles else "disabled")

    def _teardown_watcher(self) -> None:
        if self._watcher is not None:
            watched = self._watcher.files() + self._watcher.directories()
            if watched:
                self._watcher.removePaths(watched)
                logger.debug("Removed watched paths: %s", watched)
            self._watcher.deleteLater()
            self._watcher = None
            logger.debug("File watcher torn down")

    # ------------------------------------------------------------------
    # Sync orchestration
    # ------------------------------------------------------------------

    @staticmethod
    def _check_sync_folder_permissions(path: Path) -> bool:
        return os.access(path, os.R_OK | os.W_OK)

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

        if not self._check_sync_folder_permissions(sync_folder):
            logger.warning("Sync folder not readable/writable: %s", sync_folder)
            self.showMessage(
                "Chromium Profile Syncer",
                f"Cannot access sync folder: {sync_folder}\nCheck folder permissions.",
                QSystemTrayIcon.MessageIcon.Warning,
            )
            return

        logger.info("Starting sync worker")
        interval_minutes = self._config.get_sync_interval()
        self._next_sync_time = time.time() + (interval_minutes * 60)
        if self._timer is not None:
            self._timer.stop()
            self._timer.start()

        self._worker = SyncWorker(self._engine)
        self._worker.started.connect(self._on_sync_started)
        self._worker.finished.connect(self._on_sync_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._on_sync_error)
        self._worker.progress.connect(self._on_sync_progress)
        self._worker.profile_progress.connect(self._on_profile_progress)
        self._worker.start()

    def _on_sync_started(self) -> None:
        logger.debug("Sync started signal received")
        self.setIcon(self._make_icon("syncing"))
        self._action_sync.setText("⏳ Starting sync...")
        self._action_status.setText("Syncing...")
        self._action_sync.setEnabled(False)
        self._action_settings.setEnabled(False)

        self._watcher_paused = True
        logger.debug("File watcher paused during sync")

        if self._progress_dialog is None:
            self._progress_dialog = SyncProgressDialog(parent=None)
            self._progress_dialog.finished.connect(self._on_progress_dialog_closed)

        self._progress_dialog.sync_started()
        self._progress_dialog.show()
        self._progress_dialog.raise_()
        self._progress_dialog.activateWindow()

    def _on_sync_progress(self, description: str) -> None:
        truncated = description[:50] + "..." if len(description) > 50 else description
        self._action_sync.setText(f"⏳ {truncated}")
        self._action_status.setText(f"Syncing: {description}")

        if self._progress_dialog is not None:
            self._progress_dialog.on_progress(description)

    def _on_profile_progress(
        self, browser: str, profile: str, direction: str, count: int, elapsed: float
    ) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.update_profile_progress(
                browser, profile, direction, count, elapsed
            )

        if self._progress_dialog is not None:
            self._progress_dialog.update_profile_progress(
                browser, profile, direction, count, elapsed
            )

    def _on_sync_finished(self, last_sync: str, is_first_sync: bool) -> None:
        if is_first_sync:
            logger.info("Initial sync finished at %s", last_sync)
        else:
            logger.info("Sync finished at %s", last_sync)

        if self._settings_dialog is not None:
            from src import config as _config
            enabled_profiles = _config.get_enabled_profiles()
            for browser, profiles in enabled_profiles.items():
                for profile in profiles:
                    self._settings_dialog.hide_profile_progress(browser, profile)

        if self._progress_dialog is not None:
            self._progress_dialog.sync_finished(success=True)

        QTimer.singleShot(5000, self._resume_watcher)
        logger.debug("File watcher will resume in 5s")

        any_running = any(b.is_running() for b in ALL_BROWSERS)
        state = "waiting" if any_running else "idle"
        self.setIcon(self._make_icon(state))
        self._action_sync.setText("Sync Now")

        if is_first_sync:
            self._action_status.setText("Initial setup complete")
        else:
            self._action_status.setText(f"Last sync: {last_sync}")

        self._action_sync.setEnabled(True)
        self._action_settings.setEnabled(True)

    def _on_sync_error(self, msg: str) -> None:
        logger.error("Sync error: %s", msg)
        self.setIcon(self._make_icon("error"))
        self._action_sync.setText("Sync Now")
        self._action_status.setText(f"Error: {msg}")

        if self._progress_dialog is not None:
            self._progress_dialog.sync_finished(success=False)

        QTimer.singleShot(5000, self._resume_watcher)
        logger.debug("File watcher will resume in 5s (after error)")

        self._action_sync.setEnabled(True)
        self._action_settings.setEnabled(True)
