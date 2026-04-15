from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from src import autostart
from src.browser_monitor import BrowserMonitor
from src.browsers import ALL_BROWSERS
from src.dracula import ICON_COLORS
from src.settings import SettingsDialog
from src.sync_engine import SyncEngine, find_rclone

logger = logging.getLogger(__name__)


class SyncWorker(QThread):
    finished = Signal(str, bool, list, str)  # ts, is_first_sync, skipped_running, triggered_browser
    error = Signal(str)
    progress = Signal(str)  # type: ignore[assignment]
    profile_progress = Signal(str, str, str, int, float)  # browser, profile, dir, count, elapsed

    def __init__(
        self,
        engine: SyncEngine,
        only_browser: str | None = None,
        only_profile: str | None = None,
        force_direction: str | None = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._only_browser = only_browser
        self._only_profile = only_profile
        self._force_direction = force_direction
        self._current_profile: tuple[str, str, str] | None = None
        self._profile_count = 0
        self._profile_start = 0.0

    def run(self) -> None:
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
            result = self._engine.sync_all(
                only_browser=self._only_browser,
                only_profile=self._only_profile,
                force_direction=self._force_direction,
            )
            ts = datetime.now(tz=UTC).isoformat()
            is_first_sync = result.get("is_first_sync", False)
            skipped_running = result.get("skipped_running", [])
            logger.info("SyncWorker: sync finished at %s", ts)
            self.finished.emit(ts, is_first_sync, skipped_running, self._only_browser or "")
        except Exception as exc:
            logger.exception("SyncWorker: sync error")
            self.error.emit(str(exc))
        finally:
            self._engine._progress_cb = None
            self._current_profile = None


class TrayApp(QSystemTrayIcon):
    sync_completed = Signal(bool)  # True = success, False = error

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
        self._last_sync: str = self._config.get_last_sync()
        self._last_error: str = ""
        self._last_tar_mtime: float | None = None

        installed_browsers = [b for b in ALL_BROWSERS if b.is_installed()]
        self._browser_monitor = BrowserMonitor(installed_browsers, parent=self)
        self._browser_monitor.browser_closed.connect(self._on_browser_closed)
        self._browser_monitor.state_changed.connect(self._on_browser_state_changed)

        self.setIcon(self._make_icon("idle"))
        self._update_tooltip()

        self._menu = QMenu()

        self._action_settings = self._menu.addAction("Settings")
        self._action_settings.triggered.connect(self.open_settings)

        self._menu.addSeparator()

        self._action_quit = self._menu.addAction("Quit")
        self._action_quit.triggered.connect(QApplication.quit)

        self.setContextMenu(self._menu)

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
        import sys
        if sys.platform == "darwin":
            msg = "rclone is required for sync. Install via Homebrew: brew install rclone"
        elif sys.platform == "win32":
            msg = "rclone is required for sync. Download from https://rclone.org/downloads/"
        else:
            msg = "rclone is required for sync. Install: sudo apt install rclone"

        self.showMessage(
            "rclone not found",
            msg,
            QSystemTrayIcon.MessageIcon.Warning,
        )

    def _update_tooltip(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            tip = "Syncing..."
        elif self._last_error:
            tip = f"Error: {self._last_error}"
        elif self._browser_monitor.any_running():
            names = ", ".join(self._browser_monitor.running_names())
            tip = f"{names} running — syncs on close"
        elif self._last_sync:
            dt = datetime.fromisoformat(self._last_sync)
            tip = f"Last sync: {dt.strftime('%d %b %H:%M')}"
        else:
            tip = "No sync yet"
        self.setToolTip(f"Chromium Profile Syncer\n{tip}")

    # ------------------------------------------------------------------
    # Browser monitoring
    # ------------------------------------------------------------------

    def _on_browser_closed(self, browser_name: str) -> None:
        from src import config as _config

        enabled_profiles = _config.get_enabled_profiles()
        profiles = enabled_profiles.get(browser_name, [])
        if not profiles:
            return
        if not any(_config.is_profile_sync_enabled(browser_name, p) for p in profiles):
            logger.debug("Browser %s closed but auto-sync disabled for all profiles", browser_name)
            return

        logger.info("Browser %s closed — triggering sync", browser_name)
        self._trigger_sync(only_browser=browser_name)

    def _on_browser_state_changed(self, browser_name: str, is_running: bool) -> None:
        logger.debug("Browser state: %s running=%s", browser_name, is_running)
        if self._worker is None or not self._worker.isRunning():
            state = "waiting" if self._browser_monitor.any_running() else (
                "error" if self._last_error else "idle"
            )
            self.setIcon(self._make_icon(state))
        self._update_tooltip()

    # ------------------------------------------------------------------
    # File watcher (for cross-machine sync detection)
    # ------------------------------------------------------------------

    def _setup_watcher(self, sync_folder: Path) -> None:
        self._watcher = QFileSystemWatcher(self)
        if sync_folder.exists():
            self._watcher.addPaths([str(sync_folder)])
            logger.debug("Watching: %s", sync_folder)
        else:
            logger.debug("Sync folder %s does not exist yet — watcher idle", sync_folder)

        self._watcher.fileChanged.connect(self._on_file_changed)
        self._watcher.directoryChanged.connect(self._on_dir_changed)

    def _on_file_changed(self, path: str) -> None:
        if self._watcher_paused:
            return
        logger.debug("File changed: %s", path)
        self._schedule_debounced_sync()

    def _on_dir_changed(self, path: str) -> None:
        if self._watcher_paused:
            return
        logger.debug("Directory changed: %s", path)
        self._schedule_debounced_sync()

    def _schedule_debounced_sync(self) -> None:
        self._debounce_timer.start()
        logger.debug("Debounce timer reset (2s)")

    def _on_debounce_fired(self) -> None:
        if self._last_tar_mtime is not None:
            sync_folder = self._config.get_sync_folder()
            if sync_folder is not None:
                tar = sync_folder / "current.tar"
                if tar.exists():
                    try:
                        if tar.stat().st_mtime == self._last_tar_mtime:
                            logger.debug("Watcher fired but current.tar unchanged — skipping")
                            return
                    except OSError:
                        pass
        logger.info("Debounce fired — triggering sync (remote change detected)")
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

        dialog = SettingsDialog(browser_monitor=self._browser_monitor, parent=None)
        dialog.settings_saved.connect(self._on_settings_saved)
        dialog.sync_requested.connect(self._trigger_sync)
        dialog.apply_backup_requested.connect(self._trigger_apply_backup)
        self.sync_completed.connect(dialog.on_sync_completed)
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

    def _on_settings_saved(self) -> None:
        self._debounce_timer.stop()

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

    def _trigger_sync(
        self,
        only_browser: str | None = None,
        only_profile: str | None = None,
        force_direction: str | None = None,
    ) -> None:
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

        logger.info(
            "Starting sync worker (only_browser=%s, only_profile=%s, force_direction=%s)",
            only_browser, only_profile, force_direction,
        )
        self._worker = SyncWorker(self._engine, only_browser, only_profile, force_direction)
        self._worker.started.connect(self._on_sync_started)
        self._worker.finished.connect(self._on_sync_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._on_sync_error)
        self._worker.progress.connect(self._on_sync_progress)
        self._worker.profile_progress.connect(self._on_profile_progress)
        self._worker.start()

    def _trigger_apply_backup(self, browser: str, profile: str) -> None:
        logger.info("Apply backup requested for %s/%s", browser, profile)
        self._trigger_sync(only_browser=browser, only_profile=profile, force_direction="pull")

    def _on_sync_started(self) -> None:
        logger.debug("Sync started signal received")
        self.setIcon(self._make_icon("syncing"))
        self._action_settings.setEnabled(False)
        self._watcher_paused = True
        logger.debug("File watcher paused during sync")
        self._update_tooltip()

    def _on_sync_progress(self, description: str) -> None:
        pass  # tooltip not updated per-item for performance

    def _on_profile_progress(
        self, browser: str, profile: str, direction: str, count: int, elapsed: float
    ) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.update_profile_progress(
                browser, profile, direction, count, elapsed
            )

    def _on_sync_finished(
        self,
        last_sync: str,
        is_first_sync: bool,
        skipped_running: list,
        triggered_browser: str,
    ) -> None:
        self._worker = None
        self._last_error = ""
        self._last_sync = last_sync

        sync_folder = self._config.get_sync_folder()
        if sync_folder is not None:
            tar = sync_folder / "current.tar"
            try:
                self._last_tar_mtime = tar.stat().st_mtime if tar.exists() else None
            except OSError:
                self._last_tar_mtime = None
            self._config.set_last_sync(last_sync)

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

        QTimer.singleShot(5000, self._resume_watcher)
        logger.debug("File watcher will resume in 5s")

        state = "waiting" if self._browser_monitor.any_running() else "idle"
        self.setIcon(self._make_icon(state))
        self._action_settings.setEnabled(True)
        self._update_tooltip()
        self.sync_completed.emit(True)

        if skipped_running:
            names = ", ".join(skipped_running)
            self.showMessage(
                "Chromium Profile Syncer",
                f"{names} is running — close it completely to allow sync.",
                QSystemTrayIcon.MessageIcon.Warning,
            )
        elif triggered_browser and not is_first_sync:
            self.showMessage(
                "Chromium Profile Syncer",
                f"{triggered_browser} synced",
                QSystemTrayIcon.MessageIcon.NoIcon,
                2000,
            )

    def _on_sync_error(self, msg: str) -> None:
        self._worker = None
        self._last_error = msg
        logger.error("Sync error: %s", msg)
        self.setIcon(self._make_icon("error"))
        self._action_settings.setEnabled(True)
        self._update_tooltip()
        self.sync_completed.emit(False)

        QTimer.singleShot(5000, self._resume_watcher)
        logger.debug("File watcher will resume in 5s (after error)")
