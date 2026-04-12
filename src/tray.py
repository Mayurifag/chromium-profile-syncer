from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from PySide6.QtCore import QFileSystemWatcher, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from src import autostart
from src.browsers import ALL_BROWSERS
from src.log_viewer import LogViewerDialog
from src.settings import SettingsDialog
from src.sync_engine import SyncEngine
from src.sync_progress import SyncProgressDialog

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

_ICON_COLORS: dict[str, str] = {
    "idle": "#bd93f9",      # Dracula purple
    "syncing": "#8be9fd",   # Dracula cyan
    "waiting": "#f1fa8c",   # Dracula yellow
    "error": "#ff5555",     # Dracula red
}


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
            # Wire up progress callback to track per-profile progress
            def _progress_handler(desc: str) -> None:
                self.progress.emit(desc)
                # Parse "browser/profile" format
                if "/" in desc:
                    parts = desc.split("/", 1)
                    browser, profile = parts[0], parts[1]

                    # New profile started?
                    if self._current_profile is None or \
                       self._current_profile[0] != browser or \
                       self._current_profile[1] != profile:
                        # Get direction from config
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

                    # Update count
                    self._profile_count += 1
                    elapsed = datetime.now().timestamp() - self._profile_start

                    # Emit profile progress every 10 items or every second
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
        self._debounce_pending: bool = False
        self._watcher: QFileSystemWatcher | None = None
        self._watcher_paused: bool = False
        self._settings_dialog: SettingsDialog | None = None
        self._log_viewer: LogViewerDialog | None = None
        self._progress_dialog: SyncProgressDialog | None = None

        self.setIcon(self._make_icon("idle"))
        self.setToolTip("Chromium Profile Syncer")

        self._menu = QMenu()

        self._action_sync = self._menu.addAction("Sync Now")
        self._action_sync.triggered.connect(self._trigger_sync)

        self._action_settings = self._menu.addAction("Settings")
        self._action_settings.triggered.connect(self.open_settings)

        self._action_log = self._menu.addAction("Activity Log")
        self._action_log.triggered.connect(self.open_log_viewer)

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

    def _resume_watcher(self) -> None:
        """Resume file watcher after sync completes."""
        self._watcher_paused = False
        logger.debug("File watcher resumed")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def open_settings(self) -> None:
        """Open the SettingsDialog and connect its saved signal."""
        if self._settings_dialog is not None:
            # Dialog already open, just bring it to front
            self._settings_dialog.show()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return

        dialog = SettingsDialog(parent=None)
        dialog.settings_saved.connect(self._on_settings_saved)
        dialog.sync_requested.connect(self._trigger_sync)
        dialog.finished.connect(lambda: self._on_settings_closed())

        self._settings_dialog = dialog

        # macOS requires explicit activation for tray apps
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

        dialog.exec()

    def _on_settings_closed(self) -> None:
        """Clean up settings dialog reference."""
        self._settings_dialog = None

    def open_log_viewer(self) -> None:
        """Open the activity log viewer."""
        if self._log_viewer is not None:
            # Dialog already open, just bring it to front
            self._log_viewer.show()
            self._log_viewer.raise_()
            self._log_viewer.activateWindow()
            return

        self._log_viewer = LogViewerDialog(parent=None)
        self._log_viewer.finished.connect(lambda: self._on_log_viewer_closed())

        # macOS requires explicit activation for tray apps
        self._log_viewer.show()
        self._log_viewer.raise_()
        self._log_viewer.activateWindow()

    def _on_log_viewer_closed(self) -> None:
        """Clean up log viewer reference."""
        self._log_viewer = None

    def _on_progress_dialog_closed(self) -> None:
        """Clean up progress dialog reference."""
        self._progress_dialog = None

    def _on_settings_saved(self) -> None:
        """Rebuild SyncEngine and file watcher after settings are saved."""
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
        self._worker.progress.connect(self._on_sync_progress)
        self._worker.profile_progress.connect(self._on_profile_progress)
        self._worker.start()

    def _on_sync_started(self) -> None:
        logger.debug("Sync started signal received")
        self.setIcon(self._make_icon("syncing"))
        self._action_sync.setText("⏳ Starting sync...")
        self._action_status.setText("Syncing...")
        # Disable buttons during sync
        self._action_sync.setEnabled(False)
        self._action_settings.setEnabled(False)

        # Pause file watcher to prevent sync loops
        self._watcher_paused = True
        logger.debug("File watcher paused during sync")

        # Show progress dialog
        if self._progress_dialog is None:
            self._progress_dialog = SyncProgressDialog(parent=None)
            self._progress_dialog.finished.connect(self._on_progress_dialog_closed)

        self._progress_dialog.sync_started()
        self._progress_dialog.show()
        self._progress_dialog.raise_()
        self._progress_dialog.activateWindow()

    def _on_sync_progress(self, description: str) -> None:
        """Update status with current file/directory being synced."""
        # Show progress inline where the "Sync Now" button was
        truncated = description[:50] + "..." if len(description) > 50 else description
        self._action_sync.setText(f"⏳ {truncated}")
        self._action_status.setText(f"Syncing: {description}")

        # Update progress dialog
        if self._progress_dialog is not None:
            self._progress_dialog.on_progress(description)

    def _on_profile_progress(
        self, browser: str, profile: str, direction: str, count: int, elapsed: float
    ) -> None:
        """Update settings dialog with per-profile progress."""
        if self._settings_dialog is not None:
            self._settings_dialog.update_profile_progress(
                browser, profile, direction, count, elapsed
            )

        # Update progress dialog
        if self._progress_dialog is not None:
            self._progress_dialog.update_profile_progress(
                browser, profile, direction, count, elapsed
            )

    def _on_sync_finished(self, last_sync: str, is_first_sync: bool) -> None:
        if is_first_sync:
            logger.info("Initial sync finished at %s", last_sync)
        else:
            logger.info("Sync finished at %s", last_sync)

        # Hide all profile progress bars in settings dialog
        if self._settings_dialog is not None:
            from src import config as _config
            enabled_profiles = _config.get_enabled_profiles()
            for browser, profiles in enabled_profiles.items():
                for profile in profiles:
                    self._settings_dialog.hide_profile_progress(browser, profile)

        # Update progress dialog
        if self._progress_dialog is not None:
            self._progress_dialog.sync_finished(success=True)

        # Resume file watcher after cooldown (5s to let filesystem settle)
        QTimer.singleShot(5000, self._resume_watcher)
        logger.debug("File watcher will resume in 5s")

        # Check if any browser is currently running — if so, use waiting state
        any_running = any(b.is_running() for b in ALL_BROWSERS)
        state = "waiting" if any_running else "idle"
        self.setIcon(self._make_icon(state))
        self._action_sync.setText("Sync Now")

        # Only show detailed status for subsequent syncs, not first-time setup
        if is_first_sync:
            self._action_status.setText("Initial setup complete")
        else:
            self._action_status.setText(f"Last sync: {last_sync}")

        # Re-enable buttons
        self._action_sync.setEnabled(True)
        self._action_settings.setEnabled(True)

    def _on_sync_error(self, msg: str) -> None:
        logger.error("Sync error: %s", msg)
        self.setIcon(self._make_icon("error"))
        self._action_sync.setText("Sync Now")
        self._action_status.setText(f"Error: {msg}")

        # Update progress dialog
        if self._progress_dialog is not None:
            self._progress_dialog.sync_finished(success=False)

        # Resume file watcher after cooldown even on error
        QTimer.singleShot(5000, self._resume_watcher)
        logger.debug("File watcher will resume in 5s (after error)")

        # Re-enable buttons even on error
        self._action_sync.setEnabled(True)
        self._action_settings.setEnabled(True)
