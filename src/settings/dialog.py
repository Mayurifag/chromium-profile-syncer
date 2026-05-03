from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

import src.config as config_module
from src.browser_monitor import BrowserMonitor
from src.browsers import ALL_BROWSERS
from src.dracula import PROFILE_ROW_STYLE, SMALL_MUTED
from src.settings import _desktop_backup
from src.settings._activity_log import ActivityLogWidget
from src.settings._helpers import (
    _CLOSE_BROWSER_HINT,
    _make_indicator_pixmap,
    _make_status_indicator,
    _sync_folder_has_data,
    _sync_folder_has_profile,
)
from src.settings.initial_upload import InitialUploadDialog
from src.shortcuts_editor import ShortcutsEditorDialog
from src.sync.sync_dir import SYNC_DIR_NAME
from src.winget import WingetManager

_LOG = logging.getLogger(__name__)

_USAGE_NOTES_HTML = (
    "<b>Closing browsers</b><br>"
    "Sync triggers only after browser is <b>fully closed</b>.<br><br>"
    "On macOS, ✕ hides the window — use <b>⌘Q</b> to quit. "
    'Or install <a href="https://swiftquit.com">Swift Quit</a> for '
    "Windows-like behaviour (quits when last window closes).<br><br>"
    "<b>Restoring to new machine</b><br>"
    "1. Create fresh profile, close browser, run app.<br>"
    "2. Select profile → click <i>Apply Backup</i>.<br>"
    "3. Follow steps below, relaunch browser.<br><br>"
    "<b>Extensions</b><br>"
    "Go to Extensions page, enable each manually.<br><br>"
    "If none, install each from Web Store manually (app generates stubs, "
    "but ungoogled patches require user-initiated install per extension).<br><br>"
    "<b>Search shortcuts</b><br>"
    "After restoration, browsers tend to override your search default entry with theirs. "
    "Set yours as default, remove bundled shortcut."
)


class SettingsDialog(QDialog):
    settings_saved = Signal()
    sync_requested = Signal()
    apply_backup_requested = Signal(str, str)  # browser, profile

    def __init__(
        self,
        parent=None,
        *,
        browsers_list: list | None = None,
        browser_monitor: BrowserMonitor | None = None,
        winget_manager: WingetManager | None = None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Chromium Profile Syncer — Settings")
        self.setMinimumWidth(900)
        self.setSizeGripEnabled(False)

        self._browsers = (
            browsers_list
            if browsers_list is not None
            else [b for b in ALL_BROWSERS if b.is_installed()]
        )
        self._browser_monitor = browser_monitor
        self._winget_manager = winget_manager

        self._profile_states: dict[str, dict[str, bool]] = {}
        self._profile_progress: dict[tuple[str, str], tuple[QProgressBar, QLabel]] = {}
        self._autostart_check: QCheckBox | None = None
        self._helium_update_check: QCheckBox | None = None
        self._helium_version_label: QLabel | None = None
        self._folder_edit: QLineEdit | None = None
        self._clean_btn: QPushButton | None = None
        self._profiles_group: QGroupBox | None = None
        self._profiles_scroll_layout: QVBoxLayout | None = None
        self._activity_log_check: QCheckBox | None = None
        self._activity_log: ActivityLogWidget
        self._browser_status_indicators: dict[str, QLabel] = {}
        self._apply_backup_buttons: dict[tuple[str, str], QPushButton] = {}
        self._sync_toggle_buttons: dict[tuple[str, str], QPushButton] = {}
        self._remove_profile_buttons: dict[tuple[str, str], QPushButton] = {}
        self._selects_row: QWidget | None = None
        self._desktop_backup_btn: QPushButton | None = None
        self._desktop_restore_btn: QPushButton | None = None
        self._desktop_delete_btn: QPushButton | None = None
        self._syncing: bool = False

        self._build_ui()
        self._load_current_settings()
        self._connect_browser_monitor()

    def _build_ui(self) -> None:
        root = QHBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(8, 8, 8, 8)

        notes_box = QGroupBox()
        notes_box.setFixedWidth(260)
        notes_vbox = QVBoxLayout(notes_box)
        notes_vbox.setContentsMargins(4, 6, 4, 6)
        notes_label = QLabel(_USAGE_NOTES_HTML)
        notes_label.setWordWrap(True)
        notes_label.setTextFormat(Qt.TextFormat.RichText)
        notes_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        notes_label.setOpenExternalLinks(True)
        notes_vbox.addWidget(notes_label)
        notes_vbox.addStretch()
        root.addWidget(notes_box)

        right_widget = QWidget()
        right = QVBoxLayout(right_widget)
        right.setSpacing(4)
        right.setContentsMargins(0, 0, 0, 0)

        folder_group = QGroupBox("Sync folder")
        folder_layout = QHBoxLayout(folder_group)
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText("Select a folder…")
        self._folder_edit.setReadOnly(True)
        self._folder_edit.textChanged.connect(self._on_folder_changed)
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_folder)
        self._clean_btn = QPushButton("Clean")
        self._clean_btn.clicked.connect(self._clean_sync_folder)
        self._clean_btn.setVisible(False)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self._refresh_profiles)
        folder_layout.addWidget(self._folder_edit)
        folder_layout.addWidget(browse_btn)
        folder_layout.addWidget(refresh_btn)
        folder_layout.addWidget(self._clean_btn)
        right.addWidget(folder_group)

        self._profiles_group = QGroupBox("Profiles")
        self._profiles_group.setVisible(False)
        self._profiles_scroll_layout = QVBoxLayout(self._profiles_group)
        self._profiles_scroll_layout.setSpacing(1)
        right.addWidget(self._profiles_group)

        selects_row = QWidget()
        selects_layout = QHBoxLayout(selects_row)
        selects_layout.setContentsMargins(0, 0, 0, 0)
        selects_layout.setSpacing(6)

        self._activity_log_check = QCheckBox("Show activity log")
        self._activity_log_check.setChecked(True)
        self._activity_log_check.toggled.connect(self._on_activity_log_changed)
        selects_layout.addWidget(self._activity_log_check)

        selects_layout.addSpacing(8)

        self._autostart_check = QCheckBox("Launch on login")
        self._autostart_check.setChecked(True)
        self._autostart_check.toggled.connect(self._on_autostart_changed)
        selects_layout.addWidget(self._autostart_check)

        selects_layout.addSpacing(8)

        self._helium_update_check = QCheckBox("Auto-update Helium (winget)")
        self._helium_update_check.setVisible(False)
        self._helium_update_check.toggled.connect(self._on_helium_auto_update_changed)
        selects_layout.addWidget(self._helium_update_check)

        self._helium_version_label = QLabel()
        self._helium_version_label.setStyleSheet(SMALL_MUTED)
        self._helium_version_label.setVisible(False)
        selects_layout.addWidget(self._helium_version_label)

        selects_layout.addSpacing(8)

        edit_shortcuts_btn = QPushButton("Edit Search Shortcuts")
        edit_shortcuts_btn.clicked.connect(self._open_shortcuts_editor)
        selects_layout.addWidget(edit_shortcuts_btn)

        view_ext_btn = QPushButton("Extensions")
        view_ext_btn.clicked.connect(self._open_extension_links)
        selects_layout.addWidget(view_ext_btn)

        flags_btn = QPushButton("Flags")
        flags_btn.clicked.connect(self._open_flags_manager)
        selects_layout.addWidget(flags_btn)

        selects_layout.addSpacing(8)

        self._desktop_backup_btn = QPushButton("Backup to Desktop")
        self._desktop_backup_btn.clicked.connect(self._on_backup_to_desktop)
        selects_layout.addWidget(self._desktop_backup_btn)

        self._desktop_restore_btn = QPushButton("Restore Desktop Backup")
        self._desktop_restore_btn.clicked.connect(self._on_restore_desktop_backup)
        self._desktop_restore_btn.setVisible(False)
        selects_layout.addWidget(self._desktop_restore_btn)

        self._desktop_delete_btn = QPushButton("Delete Desktop Backup")
        self._desktop_delete_btn.clicked.connect(self._on_delete_desktop_backup)
        self._desktop_delete_btn.setVisible(False)
        selects_layout.addWidget(self._desktop_delete_btn)

        selects_layout.addStretch()
        selects_row.setVisible(False)
        right.addWidget(selects_row)
        self._selects_row = selects_row

        self._activity_log = ActivityLogWidget()
        self._activity_log.resized.connect(self.adjustSize)
        right.addWidget(self._activity_log)

        root.addWidget(right_widget, 1)

    def _connect_browser_monitor(self) -> None:
        if self._browser_monitor is not None:
            self._browser_monitor.state_changed.connect(self._on_browser_state_changed)
        if self._winget_manager is not None:
            self._winget_manager.detected.connect(self._on_winget_detected)
            self._on_winget_detected(
                self._winget_manager.is_managed,
                self._winget_manager.installed_version,
                self._winget_manager.available_version,
            )

    def _on_browser_state_changed(self, browser_name: str, is_running: bool) -> None:
        indicator = self._browser_status_indicators.get(browser_name)
        if indicator is not None:
            indicator.setPixmap(_make_indicator_pixmap(is_running))
            indicator.setToolTip(_CLOSE_BROWSER_HINT if is_running else "Browser is not running")
        self._refresh_apply_backup_enabled()

    def _refresh_apply_backup_enabled(self) -> None:
        for (browser_name, _profile_name), btn in self._apply_backup_buttons.items():
            is_running = (
                self._browser_monitor.is_running(browser_name) if self._browser_monitor else False
            )
            btn.setEnabled(not is_running and not self._syncing)
            if is_running:
                btn.setToolTip(_CLOSE_BROWSER_HINT)
            elif self._syncing:
                btn.setToolTip("Sync in progress")
            else:
                btn.setToolTip("")

    def on_sync_completed(self, success: bool) -> None:
        self._syncing = False
        self._refresh_apply_backup_enabled()

    def _on_folder_changed(self, text: str) -> None:
        folder_text = text.strip()
        if not folder_text or not (folder := Path(folder_text)).is_dir():
            self._hide_profiles()
            if self._clean_btn:
                self._clean_btn.setVisible(False)
            return
        config_module.set_sync_folder(folder)
        self.settings_saved.emit()
        self._refresh_for_folder(folder)

    def _refresh_for_folder(self, folder: Path) -> None:
        has_data = _sync_folder_has_data(folder)
        if self._clean_btn:
            self._clean_btn.setVisible(has_data)
        if not has_data:
            initial = self._pick_initial_upload_profile()
            if initial is None:
                self._hide_profiles()
            else:
                self._do_initial_upload(folder, initial)
        else:
            self._rebuild_profiles(folder)

    def _hide_profiles(self) -> None:
        if self._profiles_group:
            self._profiles_group.setVisible(False)
        if self._selects_row:
            self._selects_row.setVisible(False)
        self._activity_log.setVisible(False)
        self.adjustSize()

    def _on_activity_log_changed(self, checked: bool) -> None:
        if checked:
            self._activity_log.enable()
        else:
            self._activity_log.disable()

    def _on_autostart_changed(self, checked: bool) -> None:
        config_module.set_autostart(checked)
        _LOG.info("Autostart %s", "enabled" if checked else "disabled")

    def _on_helium_auto_update_changed(self, checked: bool) -> None:
        config_module.set_helium_auto_update(checked)

    def _on_winget_detected(self, managed: bool, installed: str, available: str) -> None:
        if self._helium_update_check is None:
            return
        self._helium_update_check.blockSignals(True)
        self._helium_update_check.setChecked(config_module.get_helium_auto_update())
        self._helium_update_check.setVisible(managed)
        self._helium_update_check.blockSignals(False)
        if self._helium_version_label is not None:
            if managed and installed:
                if available:
                    self._helium_version_label.setText(f"{installed} → {available}")
                else:
                    self._helium_version_label.setText(f"{installed} (latest)")
                self._helium_version_label.setVisible(True)
            else:
                self._helium_version_label.setVisible(False)

    def _pick_initial_upload_profile(self) -> tuple[str, str] | None:
        options: list[tuple[str, str, str]] = []
        for browser in self._browsers:
            for profile_path in browser.discover_profiles():
                friendly = browser.get_profile_name(profile_path)
                options.append((f"{browser.name} — {friendly}", browser.name, profile_path.name))

        if not options:
            return None

        dlg = QDialog(self)
        dlg.setWindowTitle("Initial Upload")
        dlg_layout = QVBoxLayout(dlg)
        lbl = QLabel("Sync folder is empty.\nSelect the profile to upload:")
        lbl.setWordWrap(True)
        dlg_layout.addWidget(lbl)

        combo = QComboBox()
        for display, _, _ in options:
            combo.addItem(display)
        dlg_layout.addWidget(combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dlg.accept)
        buttons.rejected.connect(dlg.reject)
        dlg_layout.addWidget(buttons)

        if dlg.exec() != QDialog.DialogCode.Accepted:
            return None

        _, browser_name, profile_name = options[combo.currentIndex()]
        return (browser_name, profile_name)

    def _find_profile_path(self, browser_name: str, profile_name: str) -> Path | None:
        for browser in self._browsers:
            if browser.name == browser_name:
                for p in browser.discover_profiles():
                    if p.name == profile_name:
                        return p
        return None

    def _do_initial_upload(self, folder: Path, initial: tuple[str, str]) -> None:
        browser_name, profile_name = initial
        profile_path = self._find_profile_path(browser_name, profile_name)
        if profile_path is None:
            self._rebuild_profiles(folder)
            return

        browser_obj = next((b for b in ALL_BROWSERS if b.name == browser_name), None)
        dlg = InitialUploadDialog(
            self,
            profile_path=profile_path,
            folder=folder,
            browser_name=browser_name,
            profile_name=profile_name,
            ext_id_aliases=browser_obj.ext_id_aliases if browser_obj else None,
        )
        dlg.upload_done.connect(
            lambda bn, pn, cnt, el: self._on_upload_done(folder, bn, pn, cnt, el)
        )
        dlg.start()

    def _on_upload_done(
        self, folder: Path, browser_name: str, profile_name: str, count: int, elapsed: float
    ) -> None:
        config_module.set_sync_folder(folder)
        config_module.set_enabled_profiles({browser_name: [profile_name]})
        config_module.set_enabled_browsers({browser_name: True})
        _LOG.info("Initial upload done: %d items in %.1fs", count, elapsed)
        if self._clean_btn:
            self._clean_btn.setVisible(_sync_folder_has_data(folder))
        self._rebuild_profiles(folder)
        self.settings_saved.emit()

    def _add_profile_row(
        self,
        layout: QVBoxLayout,
        browser_name: str,
        profile_name: str,
        folder: Path | None,
        is_running: bool,
        is_enabled: bool,
        prefix_widgets: list,
        profile_path: Path | None = None,
    ) -> None:
        row = QWidget()
        row.setObjectName("profile_row")
        row.setStyleSheet(PROFILE_ROW_STYLE)
        row_layout = QVBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(0)

        top_row = QWidget()
        top_layout = QHBoxLayout(top_row)
        top_layout.setContentsMargins(0, 0, 0, 0)
        top_layout.setSpacing(4)

        for w in prefix_widgets:
            top_layout.addWidget(w)

        if browser_name == "Thorium" and profile_path is not None:
            remove_btn = QPushButton("Remove Profile")
            remove_btn.setFixedWidth(110)
            remove_btn.setEnabled(not is_running)
            if is_running:
                remove_btn.setToolTip(_CLOSE_BROWSER_HINT)
            self._remove_profile_buttons[(browser_name, profile_name)] = remove_btn
            top_layout.addWidget(remove_btn)

            def _on_remove_clicked(
                checked: bool = False,
                bn: str = browser_name,
                pn: str = profile_name,
                pp: Path = profile_path,
            ) -> None:
                self._do_remove_profile(bn, pn, pp)

            remove_btn.clicked.connect(_on_remove_clicked)

        top_layout.addStretch()

        sync_toggle_btn = QPushButton()
        sync_toggle_btn.setFixedWidth(110)
        sync_enabled = config_module.is_profile_sync_enabled(browser_name, profile_name)
        sync_toggle_btn.setText("Auto-sync: ON" if sync_enabled else "Auto-sync: OFF")
        sync_toggle_btn.setVisible(is_enabled)
        self._sync_toggle_buttons[(browser_name, profile_name)] = sync_toggle_btn
        top_layout.addWidget(sync_toggle_btn)

        apply_btn = QPushButton("Apply Backup")
        apply_btn.setFixedWidth(100)
        apply_btn.setEnabled(not is_running and not self._syncing)
        if is_running:
            apply_btn.setToolTip(_CLOSE_BROWSER_HINT)
        self._apply_backup_buttons[(browser_name, profile_name)] = apply_btn
        top_layout.addWidget(apply_btn)

        progress_bar = QProgressBar()
        progress_bar.setMaximumHeight(8)
        progress_bar.setTextVisible(False)
        progress_bar.setVisible(False)

        info_label = QLabel()
        info_label.setStyleSheet(SMALL_MUTED)
        info_label.setVisible(False)

        self._profile_progress[(browser_name, profile_name)] = (progress_bar, info_label)

        row_layout.addWidget(top_row)
        row_layout.addWidget(progress_bar)
        row_layout.addWidget(info_label)
        layout.addWidget(row)

        def _on_toggle_clicked(
            checked: bool = False,
            bn: str = browser_name,
            pn: str = profile_name,
            btn: QPushButton = sync_toggle_btn,
        ) -> None:
            enabled = config_module.is_profile_sync_enabled(bn, pn)
            config_module.set_profile_sync_enabled(bn, pn, not enabled)
            btn.setText("Auto-sync: ON" if not enabled else "Auto-sync: OFF")

        sync_toggle_btn.clicked.connect(_on_toggle_clicked)

        def _on_apply_clicked(
            checked: bool = False,
            bn: str = browser_name,
            pn: str = profile_name,
            s_btn: QPushButton = sync_toggle_btn,
            f: Path | None = folder,
        ) -> None:
            _LOG.debug("Apply Backup clicked: %s/%s", bn, pn)
            try:
                currently = self._profile_states[bn][pn]
            except KeyError:
                _LOG.error("Apply Backup: profile state missing for %s/%s", bn, pn)
                return
            if not currently:
                self._profile_states[bn][pn] = True
                s_btn.setVisible(True)
                if f is not None and _sync_folder_has_profile(f):
                    config_module.mark_profile_for_restore(bn, pn)
                    _LOG.info("Profile %s/%s marked for initial restore", bn, pn)
                self._save_profiles_config()
                self._syncing = True
                self._refresh_apply_backup_enabled()
                self.apply_backup_requested.emit(bn, pn)
            else:
                from PySide6.QtWidgets import QMessageBox

                reply = QMessageBox.question(
                    self,
                    "Apply Backup",
                    f"Overwrite local {bn} profile with backup?\n\n"
                    "This will replace local data with the synced backup.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._syncing = True
                    self._refresh_apply_backup_enabled()
                    self.apply_backup_requested.emit(bn, pn)

        apply_btn.clicked.connect(_on_apply_clicked)

    def _rebuild_profiles(self, folder: Path | None) -> None:
        layout = self._profiles_scroll_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._profile_states.clear()
        self._profile_progress.clear()
        self._browser_status_indicators.clear()
        self._apply_backup_buttons.clear()
        self._sync_toggle_buttons.clear()
        self._remove_profile_buttons.clear()

        saved_profiles = config_module.get_enabled_profiles()
        found_any = False

        for browser in self._browsers:
            profiles = browser.discover_profiles()
            if not profiles:
                continue
            found_any = True

            self._profile_states[browser.name] = {}
            browser_saved = set(saved_profiles.get(browser.name, []))
            is_running = (
                self._browser_monitor.is_running(browser.name)
                if self._browser_monitor
                else browser.is_running()
            )

            if len(profiles) == 1:
                profile_name = profiles[0].name
                is_enabled = profile_name in browser_saved
                self._profile_states[browser.name][profile_name] = is_enabled

                indicator = _make_status_indicator(is_running)
                self._browser_status_indicators[browser.name] = indicator
                self._add_profile_row(
                    layout,
                    browser.name,
                    profile_name,
                    folder,
                    is_running,
                    is_enabled,
                    prefix_widgets=[indicator, QLabel(f"<b>{browser.name}</b>")],
                    profile_path=profiles[0],
                )
            else:
                header_row = QWidget()
                header_row.setObjectName("profile_row")
                header_row.setStyleSheet(PROFILE_ROW_STYLE)
                header_layout = QHBoxLayout(header_row)
                header_layout.setContentsMargins(0, 0, 0, 0)
                header_layout.setSpacing(4)

                indicator = _make_status_indicator(is_running)
                self._browser_status_indicators[browser.name] = indicator
                header_layout.addWidget(indicator)
                header_layout.addWidget(QLabel(f"<b>{browser.name}</b>"))
                header_layout.addStretch()
                layout.addWidget(header_row)

                for profile_path in profiles:
                    profile_name = profile_path.name
                    friendly = browser.get_profile_name(profile_path)
                    display = (
                        f"{friendly}  ({profile_name})"
                        if friendly != profile_name
                        else profile_name
                    )
                    is_enabled = profile_name in browser_saved
                    self._profile_states[browser.name][profile_name] = is_enabled

                    spacer = QLabel()
                    spacer.setFixedWidth(12)
                    self._add_profile_row(
                        layout,
                        browser.name,
                        profile_name,
                        folder,
                        is_running,
                        is_enabled,
                        prefix_widgets=[spacer, QLabel(f"• {display}")],
                        profile_path=profile_path,
                    )

        if not found_any:
            layout.addWidget(QLabel("No supported browsers detected."))

        layout.addStretch()

        if self._profiles_group:
            self._profiles_group.setVisible(True)
        if self._selects_row:
            self._selects_row.setVisible(True)
        if self._activity_log_check and self._activity_log_check.isChecked():
            self._activity_log.enable()

        self._refresh_desktop_backup_buttons()
        self.adjustSize()

    def _save_profiles_config(self) -> None:
        enabled_profiles: dict[str, list[str]] = {
            bn: [pn for pn, on in pm.items() if on] for bn, pm in self._profile_states.items()
        }
        enabled_browsers: dict[str, bool] = {
            bn: any(pm.values()) for bn, pm in self._profile_states.items()
        }
        data = config_module.load()
        data["enabled_profiles"] = enabled_profiles
        data["enabled_browsers"] = enabled_browsers
        config_module.save(data)
        _LOG.info("Profile configuration updated")

    def _refresh_profiles(self) -> None:
        folder_text = self._folder_edit.text().strip() if self._folder_edit else ""
        if folder_text and (folder := Path(folder_text)).is_dir():
            self._rebuild_profiles(folder)

    def _do_remove_profile(self, browser_name: str, profile_name: str, profile_path: Path) -> None:
        import shutil

        from PySide6.QtWidgets import QMessageBox

        is_running = (
            self._browser_monitor.is_running(browser_name) if self._browser_monitor else False
        )
        if is_running:
            QMessageBox.warning(self, "Browser Running", _CLOSE_BROWSER_HINT)
            return

        reply = QMessageBox.question(
            self,
            "Remove Profile",
            f"Permanently delete {browser_name} profile '{profile_name}' from disk?\n\n"
            f"{profile_path}\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        if profile_path.is_dir():
            shutil.rmtree(profile_path)
            _LOG.info("Deleted profile directory: %s", profile_path)

        config_module.remove_browser_profile(browser_name)

        folder_text = self._folder_edit.text().strip() if self._folder_edit else ""
        if folder_text:
            self._folder_edit.textChanged.emit(folder_text)

    def update_profile_progress(
        self, browser: str, profile: str, direction: str, count: int, elapsed: float
    ) -> None:
        key = (browser, profile)
        if key not in self._profile_progress:
            return
        progress_bar, info_label = self._profile_progress[key]
        progress_bar.setRange(0, 0)
        progress_bar.setVisible(True)
        rate = count / elapsed if elapsed > 0.1 else 0
        info_text = f"{direction}: {count} items • {elapsed:.0f}s"
        if rate > 0:
            info_text += f" • ~{rate:.1f} items/s"
        info_label.setText(info_text)
        info_label.setVisible(True)

    def hide_profile_progress(self, browser: str, profile: str) -> None:
        key = (browser, profile)
        if key not in self._profile_progress:
            return
        progress_bar, info_label = self._profile_progress[key]
        progress_bar.setVisible(False)
        info_label.setVisible(False)

    def _browse_folder(self) -> None:
        current = self._folder_edit.text() if self._folder_edit else ""
        chosen = QFileDialog.getExistingDirectory(self, "Select sync folder", current)
        if chosen and self._folder_edit:
            self._folder_edit.setText(chosen)

    def _clean_sync_folder(self, *, skip_confirmation: bool = False) -> None:
        from PySide6.QtWidgets import QMessageBox

        folder_text = self._folder_edit.text().strip() if self._folder_edit else ""
        if not folder_text:
            return
        folder = Path(folder_text)
        if not folder.is_dir():
            return

        if not skip_confirmation:
            reply = QMessageBox.question(
                self,
                "Clean Sync Folder",
                f"This will delete all synced data in:\n{folder}\n\n"
                "Are you sure you want to start from scratch?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        target = folder / SYNC_DIR_NAME
        errors: list[tuple[str, BaseException]] = []

        def _onexc(_func, path, exc):
            errors.append((str(path), exc))

        if target.is_dir():
            import shutil

            shutil.rmtree(target, onexc=_onexc)

        if target.exists() or errors:
            details = "\n".join(f"{p}: {e}" for p, e in errors[:5]) or str(target)
            extra = f"\n(+{len(errors) - 5} more)" if len(errors) > 5 else ""
            QMessageBox.warning(
                self,
                "Clean Sync Folder",
                "Could not fully delete sync folder. "
                "Files may be locked by a mount (OpenCloud/FUSE) or another process. "
                f"Close it and retry.\n\n{details}{extra}",
            )
            _LOG.warning("Clean sync folder incomplete: %s (errors=%d)", target, len(errors))
            return

        config_module.set_enabled_profiles({})
        config_module.set_enabled_browsers({})
        _LOG.info("Sync folder cleaned: %s", folder)

        if self._folder_edit:
            self._folder_edit.textChanged.emit(folder_text)

    def _get_sync_dir_or_warn(self) -> Path | None:
        from PySide6.QtWidgets import QMessageBox

        sync_folder = config_module.get_sync_folder()
        if not sync_folder or not sync_folder.exists():
            QMessageBox.warning(self, "No Sync Folder", "Please configure a sync folder first.")
            return None
        current_dir = sync_folder / SYNC_DIR_NAME
        if not current_dir.is_dir():
            QMessageBox.information(
                self,
                "No Backup",
                "No backup found.\n\nRun a sync first to create the backup.",
            )
            return None
        return current_dir

    def _open_shortcuts_editor(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        current_dir = self._get_sync_dir_or_warn()
        if current_dir is None:
            return

        shortcuts_json_path = current_dir / "search_shortcuts.json"
        if not shortcuts_json_path.exists():
            QMessageBox.information(
                self,
                "No Shortcuts Yet",
                "Search shortcuts haven't been extracted yet.\n\n"
                "They will be created on the next sync.",
            )
            return
        editor = ShortcutsEditorDialog(self, shortcuts_json_path=shortcuts_json_path)
        editor.exec()

    def _open_extension_links(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from src.settings.extensions_manager import ExtensionsManagerDialog

        current_dir = self._get_sync_dir_or_warn()
        if current_dir is None:
            return

        try:
            dlg = ExtensionsManagerDialog(self, sync_folder=current_dir.parent)
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to open extensions manager:\n{exc}")
            return
        dlg.exec()

    def _open_flags_manager(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from src.settings.flags_manager import FlagsManagerDialog

        current_dir = self._get_sync_dir_or_warn()
        if current_dir is None:
            return

        try:
            dlg = FlagsManagerDialog(self, sync_folder=current_dir.parent)
        except Exception as exc:
            QMessageBox.critical(self, "Error", f"Failed to open flags manager:\n{exc}")
            return
        dlg.exec()

    def _refresh_desktop_backup_buttons(self) -> None:
        if self._desktop_backup_btn is None:
            return
        sync_folder = config_module.get_sync_folder()
        has_current = sync_folder is not None and (sync_folder / SYNC_DIR_NAME).is_dir()
        self._desktop_backup_btn.setEnabled(has_current)
        exists = _desktop_backup.desktop_backup_path().is_file()
        self._desktop_restore_btn.setVisible(exists)
        self._desktop_delete_btn.setVisible(exists)

    def _on_backup_to_desktop(self) -> None:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QApplication, QMessageBox

        sync_folder = config_module.get_sync_folder()
        if sync_folder is None:
            QMessageBox.warning(self, "No Sync Folder", "Configure a sync folder first.")
            return
        current_dir = sync_folder / SYNC_DIR_NAME
        if not current_dir.is_dir():
            QMessageBox.information(self, "No Backup", "Run a sync first to create the backup.")
            return

        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        self._desktop_backup_btn.setEnabled(False)
        QCoreApplication.processEvents()
        try:
            target = _desktop_backup.create(current_dir)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self._desktop_backup_btn.setEnabled(True)
            _LOG.exception("Desktop backup failed")
            QMessageBox.critical(self, "Backup Failed", f"Could not create backup:\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        _LOG.info("Desktop backup written: %s", target)
        self._refresh_desktop_backup_buttons()
        QMessageBox.information(self, "Backup Created", f"Saved to:\n{target}")

    def _on_restore_desktop_backup(self) -> None:
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtGui import QCursor
        from PySide6.QtWidgets import QApplication, QMessageBox

        sync_folder = config_module.get_sync_folder()
        if sync_folder is None:
            QMessageBox.warning(self, "No Sync Folder", "Configure a sync folder first.")
            return

        backup_path = _desktop_backup.desktop_backup_path()
        if not backup_path.is_file():
            self._refresh_desktop_backup_buttons()
            return

        reply = QMessageBox.question(
            self,
            "Restore Desktop Backup",
            f"Overwrite sync folder contents with desktop backup?\n\n"
            f"From: {backup_path}\nTo:   {sync_folder / SYNC_DIR_NAME}\n\n"
            "Existing synced data will be replaced.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        QCoreApplication.processEvents()
        try:
            _desktop_backup.restore(sync_folder / SYNC_DIR_NAME)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            _LOG.exception("Desktop backup restore failed")
            QMessageBox.critical(self, "Restore Failed", f"Could not restore backup:\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        _LOG.info("Desktop backup restored to %s", sync_folder / SYNC_DIR_NAME)
        self._refresh_for_folder(sync_folder)
        QMessageBox.information(self, "Restore Complete", "Desktop backup restored.")

    def _on_delete_desktop_backup(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        backup_path = _desktop_backup.desktop_backup_path()
        if not backup_path.is_file():
            self._refresh_desktop_backup_buttons()
            return

        reply = QMessageBox.question(
            self,
            "Delete Desktop Backup",
            f"Delete {backup_path}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            _desktop_backup.delete()
        except Exception as exc:
            _LOG.exception("Desktop backup delete failed")
            QMessageBox.critical(self, "Delete Failed", f"Could not delete backup:\n{exc}")
            return
        _LOG.info("Desktop backup deleted")
        self._refresh_desktop_backup_buttons()

    def closeEvent(self, event) -> None:  # noqa: N802
        self._activity_log.cleanup()
        if self._browser_monitor is not None:
            try:
                self._browser_monitor.state_changed.disconnect(self._on_browser_state_changed)
            except RuntimeError:
                pass
        if self._winget_manager is not None:
            try:
                self._winget_manager.detected.disconnect(self._on_winget_detected)
            except RuntimeError:
                pass
        super().closeEvent(event)

    def _load_current_settings(self) -> None:
        sync_folder = config_module.get_sync_folder()
        if sync_folder and self._folder_edit:
            self._folder_edit.blockSignals(True)
            self._folder_edit.setText(str(sync_folder))
            self._folder_edit.blockSignals(False)
            if sync_folder.is_dir():
                self._refresh_for_folder(sync_folder)
            else:
                self._hide_profiles()
        else:
            self._hide_profiles()

        if self._autostart_check is not None:
            self._autostart_check.setChecked(config_module.get_autostart())
