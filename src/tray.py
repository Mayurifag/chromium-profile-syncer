from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

import src.config as _config
from src import autostart, updater
from src.browser_monitor import BrowserMonitor
from src.browsers import ALL_BROWSERS
from src.dracula import APP_ICON_SVG, ICON_COLORS
from src.notify import notify
from src.rclone import find_rclone
from src.settings import SettingsDialog
from src.sync.sync_dir import SYNC_DIR_NAME as _SYNC_DIR_NAME
from src.sync_engine import SyncEngine
from src.sync_worker import SyncWorker
from src.update_runner import UpdateCheckThread, is_update_supported
from src.watcher_controller import WatcherController
from src.winget import WingetManager

UPDATE_CHECK_INTERVAL_MS = 60 * 60 * 1000

logger = logging.getLogger(__name__)


def make_app_icon() -> QIcon:
    renderer = QSvgRenderer(QByteArray(APP_ICON_SVG.encode()))
    pixmap = QPixmap(256, 256)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


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
        self._watcher = WatcherController(_SYNC_DIR_NAME, parent=self)
        self._watcher.change_detected.connect(self._on_watcher_change)
        self._settings_dialog: SettingsDialog | None = None
        self._last_sync: str = self._config.get_last_sync()
        self._last_error: str = ""
        self._update_thread: UpdateCheckThread | None = None

        installed_browsers = [b for b in ALL_BROWSERS if b.is_installed()]
        self._browser_monitor = BrowserMonitor(installed_browsers, parent=self)
        self._browser_monitor.browser_closed.connect(self._on_browser_closed)
        self._browser_monitor.state_changed.connect(self._on_browser_state_changed)

        self.setIcon(self._make_icon("idle"))
        self._update_tooltip()

        self._menu = QMenu()
        self._action_settings = self._menu.addAction("Settings")
        self._action_settings.triggered.connect(self.open_settings)
        if is_update_supported():
            self._action_check_updates = self._menu.addAction("Check for updates")
            self._action_check_updates.triggered.connect(self._manual_update_check)
        self._menu.addSeparator()
        self._action_quit = self._menu.addAction("Quit")
        self._action_quit.triggered.connect(QApplication.quit)
        self.setContextMenu(self._menu)

        if is_update_supported():
            updater.cleanup_staging()
            self._update_timer = QTimer(self)
            self._update_timer.setInterval(UPDATE_CHECK_INTERVAL_MS)
            self._update_timer.timeout.connect(self._auto_update_check)
            self._update_timer.start()

        self._periodic_sync_timer = QTimer(self)
        self._periodic_sync_timer.timeout.connect(self._on_periodic_sync_tick)
        self._restart_periodic_sync_timer()

        self._watcher.update_sync_folder(self._config.get_sync_folder())

        self._winget = WingetManager(self)
        self._winget.detected.connect(self._on_winget_detected)
        self._winget.upgrade_finished.connect(self._on_helium_upgrade_finished)

        QTimer.singleShot(2000, self._auto_update_check)

        logger.debug("TrayApp initialized")

        if find_rclone() is None:
            QTimer.singleShot(500, self._warn_rclone_missing)

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
        notify("rclone not found", msg)

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

    def _on_browser_closed(self, browser_name: str) -> None:
        enabled_profiles = _config.get_enabled_profiles()
        profiles = enabled_profiles.get(browser_name, [])
        if not profiles:
            return
        # ext_repull-pending profiles must sync on close even with auto-sync
        # disabled — extensions installed by policy on first launch overwrite
        # restored LES with defaults, and only this post-launch sync re-applies
        # the synced data back over those defaults.
        pending_repull = _config.get_profiles_needing_ext_repull().get(browser_name, [])
        has_pending = any(p in pending_repull for p in profiles)
        if not has_pending and not any(
            _config.is_profile_sync_enabled(browser_name, p) for p in profiles
        ):
            logger.debug("Browser %s closed but auto-sync disabled for all profiles", browser_name)
            return

        logger.info("Browser %s closed — triggering sync", browser_name)
        self._trigger_sync(only_browser=browser_name)

    def _on_browser_state_changed(self, browser_name: str, is_running: bool) -> None:
        logger.debug("Browser state: %s running=%s", browser_name, is_running)
        if self._worker is None or not self._worker.isRunning():
            state = (
                "waiting"
                if self._browser_monitor.any_running()
                else ("error" if self._last_error else "idle")
            )
            self.setIcon(self._make_icon(state))
        self._update_tooltip()

    def _on_watcher_change(self) -> None:
        if self._browser_monitor.any_running():
            logger.debug("Watcher fired but a browser is running — deferring to browser close")
            return

        enabled_profiles = _config.get_enabled_profiles()
        if not any(
            _config.is_profile_sync_enabled(browser, profile)
            for browser, profiles in enabled_profiles.items()
            for profile in profiles
        ):
            logger.debug("Watcher fired but no profiles have auto-sync enabled — skipping")
            return

        logger.info("Debounce fired — triggering sync (remote change detected)")
        self._trigger_sync()

    def _restart_periodic_sync_timer(self) -> None:
        minutes = max(1, self._config.get_sync_interval())
        self._periodic_sync_timer.stop()
        self._periodic_sync_timer.setInterval(minutes * 60 * 1000)
        self._periodic_sync_timer.start()
        logger.debug("Periodic sync timer set to %d min", minutes)

    def _on_periodic_sync_tick(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if self._browser_monitor.any_running():
            logger.debug("Periodic tick — browser running, deferring")
            return
        if self._config.get_sync_folder() is None:
            return
        enabled_profiles = _config.get_enabled_profiles()
        if not any(
            _config.is_profile_sync_enabled(browser, profile)
            for browser, profiles in enabled_profiles.items()
            for profile in profiles
        ):
            return
        logger.info("Periodic sync tick — triggering sync")
        self._trigger_sync()

    def open_settings(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.show()
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return

        dialog = SettingsDialog(
            browser_monitor=self._browser_monitor,
            winget_manager=self._winget,
            parent=None,
        )
        dialog.settings_saved.connect(self._on_settings_saved)
        dialog.sync_requested.connect(self._trigger_sync)
        dialog.apply_backup_requested.connect(self._trigger_apply_backup)
        self.sync_completed.connect(dialog.on_sync_completed)
        dialog.finished.connect(lambda: self._on_settings_closed())

        self._settings_dialog = dialog

        if find_rclone() is None:
            self._warn_rclone_missing()

        dialog.exec()

    def _on_settings_closed(self) -> None:
        self._settings_dialog = None

    def _on_settings_saved(self) -> None:
        self._watcher.stop_debounce()

        sync_folder = self._config.get_sync_folder()
        if sync_folder is None:
            logger.warning("Settings saved but no sync folder chosen — keeping existing engine")
            return

        self._engine = SyncEngine(sync_folder)
        logger.info("Settings saved — engine rebuilt with folder %s", sync_folder)

        self._watcher.update_sync_folder(sync_folder)
        self._watcher.record_mtime_baseline()
        self._restart_periodic_sync_timer()

        has_profiles = any(profiles for profiles in self._config.get_enabled_profiles().values())
        autostart.apply(has_profiles)
        logger.info("Autostart %s", "enabled" if has_profiles else "disabled")

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
            notify(
                "Chromium Profile Syncer",
                "No sync folder configured. Open Settings to choose one.",
            )
            return

        if not self._check_sync_folder_permissions(sync_folder):
            logger.warning("Sync folder not readable/writable: %s", sync_folder)
            notify(
                "Chromium Profile Syncer",
                f"Cannot access sync folder: {sync_folder}\nCheck folder permissions.",
            )
            return

        logger.info(
            "Starting sync worker (only_browser=%s, only_profile=%s, force_direction=%s)",
            only_browser,
            only_profile,
            force_direction,
        )
        self._worker = SyncWorker(self._engine, only_browser, only_profile, force_direction)
        self._worker.started.connect(self._on_sync_started)
        self._worker.finished.connect(self._on_sync_finished)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.error.connect(self._on_sync_error)
        self._worker.profile_progress.connect(self._on_profile_progress)
        self._worker.start()

    def _trigger_apply_backup(self, browser: str, profile: str) -> None:
        logger.info("Apply backup requested for %s/%s", browser, profile)
        self._trigger_sync(only_browser=browser, only_profile=profile, force_direction="pull")

    def _on_sync_started(self) -> None:
        self.setIcon(self._make_icon("syncing"))
        self._action_settings.setEnabled(False)
        self._watcher.pause()
        self._update_tooltip()

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

        if self._config.get_sync_folder() is not None:
            self._watcher.record_mtime_baseline()
            self._config.set_last_sync(last_sync)

        if is_first_sync:
            logger.info("Initial sync finished at %s", last_sync)
        else:
            logger.info("Sync finished at %s", last_sync)

        if self._settings_dialog is not None:
            enabled_profiles = _config.get_enabled_profiles()
            for browser, profiles in enabled_profiles.items():
                for profile in profiles:
                    self._settings_dialog.hide_profile_progress(browser, profile)

        QTimer.singleShot(5000, self._watcher.resume)

        state = "waiting" if self._browser_monitor.any_running() else "idle"
        self.setIcon(self._make_icon(state))
        self._action_settings.setEnabled(True)
        self._update_tooltip()
        self.sync_completed.emit(True)

        if skipped_running:
            names = ", ".join(skipped_running)
            notify(
                "Chromium Profile Syncer",
                f"{names} is running — close it completely to allow sync.",
            )
        elif triggered_browser and not is_first_sync:
            notify("Chromium Profile Syncer", f"{triggered_browser} synced", duration_ms=2000)

    def _auto_update_check(self) -> None:
        if is_update_supported():
            if self._settings_dialog is None:
                self._do_update_check(silent=True)
            else:
                logger.debug("update: skip app update check — settings dialog open")
        self._winget.detect()

    def _manual_update_check(self) -> None:
        self._do_update_check(silent=False)

    def _do_update_check(self, silent: bool) -> None:
        if self._update_thread is not None and self._update_thread.isRunning():
            logger.debug("update: check already running")
            return
        thread = UpdateCheckThread(
            silent=silent,
            is_sync_running=lambda: self._worker is not None and self._worker.isRunning(),
            parent=self,
        )
        thread.user_message.connect(lambda msg: notify("Chromium Profile Syncer", msg))
        thread.completed.connect(self._on_update_completed)
        thread.finished.connect(thread.deleteLater)
        self._update_thread = thread
        thread.start()

    def _on_update_completed(self, should_quit: bool) -> None:
        self._update_thread = None
        if should_quit:
            QApplication.quit()

    def _on_winget_detected(self, managed: bool, _installed: str, available: str) -> None:
        if not managed:
            return
        if not self._config.get_helium_auto_update():
            return
        if not available:
            return
        if self._browser_monitor.is_running("Helium"):
            logger.debug("winget upgrade Helium: skipped — browser running")
            return
        self._winget.upgrade()

    def _on_helium_upgrade_finished(self, success: bool, message: str) -> None:
        notify("Chromium Profile Syncer", message, duration_ms=3000)

    def _on_sync_error(self, msg: str) -> None:
        self._worker = None
        self._last_error = msg
        logger.error("Sync error: %s", msg)
        self.setIcon(self._make_icon("error"))
        self._action_settings.setEnabled(True)
        self._update_tooltip()
        self.sync_completed.emit(False)
        QTimer.singleShot(5000, self._watcher.resume)
