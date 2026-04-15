from __future__ import annotations

import logging
import platform
import time
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap, QTextCursor
from PySide6.QtWidgets import (
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
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import src.config as config_module
from src.browser_monitor import BrowserMonitor
from src.browsers import ALL_BROWSERS
from src.dracula import (
    DEFAULT_LOG_COLOR,
    LOG_COLORS,
    NOT_RUNNING_DOT,
    PROFILE_ROW_STYLE,
    RUNNING_DOT,
    RUNNING_GLOW,
    SMALL_MUTED,
)
from src.log_viewer import GUILogHandler, LogSignaler
from src.shortcuts_editor import ShortcutsEditorDialog
from src.sync_worker import _InitialUploadWorker

_LOG = logging.getLogger(__name__)

_CLOSE_BROWSER_HINT = (
    "Close browser from the system tray to allow sync"
    if platform.system() == "Windows"
    else "Quit browser (Cmd+Q) to allow sync"
)


def _make_indicator_pixmap(is_running: bool) -> QPixmap:
    pixmap = QPixmap(12, 12)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if is_running:
        painter.setBrush(QColor(*RUNNING_GLOW))
        painter.drawEllipse(0, 0, 12, 12)
        painter.setBrush(QColor(*RUNNING_DOT))
        painter.drawEllipse(2, 2, 8, 8)
    else:
        painter.setBrush(QColor(*NOT_RUNNING_DOT))
        painter.drawEllipse(2, 2, 8, 8)
    painter.end()
    return pixmap


def _make_status_indicator(is_running: bool) -> QLabel:
    label = QLabel()
    label.setPixmap(_make_indicator_pixmap(is_running))
    label.setToolTip(_CLOSE_BROWSER_HINT if is_running else "Browser is not running")
    return label


def _sync_folder_has_data(folder: Path) -> bool:
    if (folder / "current.tar").is_file():
        return True
    current = folder / "current"
    if not current.is_dir():
        return False
    return any(entry.is_dir() for entry in current.iterdir())


def _sync_folder_has_profile(folder: Path) -> bool:
    if (folder / "current.tar").is_file():
        return True
    current = folder / "current"
    return current.is_dir() and any(current.iterdir())


class SettingsDialog(QDialog):
    settings_saved = Signal()
    sync_requested = Signal()
    apply_backup_requested = Signal(str, str)  # browser, profile

    def __init__(self, parent=None, *, browsers_list: list | None = None,
                 browser_monitor: BrowserMonitor | None = None):
        super().__init__(parent)
        self.setWindowTitle("Chromium Profile Syncer — Settings")
        self.setMinimumWidth(400)
        self.setSizeGripEnabled(False)

        self._browsers = (
            browsers_list if browsers_list is not None
            else [b for b in ALL_BROWSERS if b.is_installed()]
        )
        self._browser_monitor = browser_monitor

        self._profile_states: dict[str, dict[str, bool]] = {}
        self._profile_progress: dict[tuple[str, str], tuple[QProgressBar, QLabel]] = {}
        self._autostart_select: QComboBox | None = None
        self._folder_edit: QLineEdit | None = None
        self._clean_btn: QPushButton | None = None
        self._profiles_group: QGroupBox | None = None
        self._profiles_scroll_layout: QVBoxLayout | None = None
        self._activity_log_select: QComboBox | None = None
        self._activity_log_widget: QWidget | None = None
        self._activity_log_text: QTextEdit | None = None
        self._clear_log_btn: QPushButton | None = None
        self._log_signaler: LogSignaler | None = None
        self._log_handler: GUILogHandler | None = None
        self._browser_status_indicators: dict[str, QLabel] = {}
        self._apply_backup_buttons: dict[tuple[str, str], QPushButton] = {}
        self._sync_toggle_buttons: dict[tuple[str, str], QPushButton] = {}
        self._selects_row: QWidget | None = None
        self._syncing: bool = False

        self._build_ui()
        self._load_current_settings()
        self._connect_browser_monitor()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(8, 8, 8, 8)

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
        folder_layout.addWidget(self._folder_edit)
        folder_layout.addWidget(browse_btn)
        folder_layout.addWidget(self._clean_btn)
        root.addWidget(folder_group)

        self._profiles_group = QGroupBox("Profiles")
        self._profiles_group.setVisible(False)
        self._profiles_scroll_layout = QVBoxLayout(self._profiles_group)
        self._profiles_scroll_layout.setSpacing(1)

        root.addWidget(self._profiles_group)

        selects_row = QWidget()
        selects_layout = QHBoxLayout(selects_row)
        selects_layout.setContentsMargins(0, 0, 0, 0)
        selects_layout.setSpacing(6)

        selects_layout.addWidget(QLabel("Show activity log:"))
        self._activity_log_select = QComboBox()
        self._activity_log_select.addItem("Yes", True)
        self._activity_log_select.addItem("No", False)
        self._activity_log_select.setCurrentIndex(0)
        self._activity_log_select.currentIndexChanged.connect(self._on_activity_log_changed)
        selects_layout.addWidget(self._activity_log_select)

        selects_layout.addSpacing(8)

        selects_layout.addWidget(QLabel("Launch on login:"))
        self._autostart_select = QComboBox()
        self._autostart_select.addItem("Yes", True)
        self._autostart_select.addItem("No", False)
        self._autostart_select.setCurrentIndex(0)
        self._autostart_select.currentIndexChanged.connect(self._on_autostart_changed)
        selects_layout.addWidget(self._autostart_select)

        selects_layout.addSpacing(8)

        edit_shortcuts_btn = QPushButton("Edit Search Shortcuts")
        edit_shortcuts_btn.clicked.connect(self._open_shortcuts_editor)
        selects_layout.addWidget(edit_shortcuts_btn)

        selects_layout.addStretch()
        selects_row.setVisible(False)
        root.addWidget(selects_row)
        self._selects_row = selects_row

        self._activity_log_widget = QWidget()
        log_layout = QVBoxLayout(self._activity_log_widget)
        log_layout.setContentsMargins(0, 0, 0, 0)

        self._activity_log_text = QTextEdit()
        self._activity_log_text.setReadOnly(True)
        self._activity_log_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._activity_log_text.setMinimumHeight(200)
        self._activity_log_text.setMaximumHeight(300)

        font = QFont("Monaco, Menlo, Courier New, monospace")
        font.setPointSize(11)
        self._activity_log_text.setFont(font)

        log_layout.addWidget(self._activity_log_text)

        self._clear_log_btn = QPushButton("Clear Log")
        self._clear_log_btn.clicked.connect(self._clear_activity_log)
        self._clear_log_btn.setVisible(False)
        log_layout.addWidget(self._clear_log_btn)

        self._activity_log_widget.setVisible(False)
        root.addWidget(self._activity_log_widget)

    def _connect_browser_monitor(self) -> None:
        if self._browser_monitor is not None:
            self._browser_monitor.state_changed.connect(self._on_browser_state_changed)

    def _on_browser_state_changed(self, browser_name: str, is_running: bool) -> None:
        indicator = self._browser_status_indicators.get(browser_name)
        if indicator is not None:
            indicator.setPixmap(_make_indicator_pixmap(is_running))
            indicator.setToolTip(_CLOSE_BROWSER_HINT if is_running else "Browser is not running")
        self._refresh_apply_backup_enabled()

    def _refresh_apply_backup_enabled(self) -> None:
        for (browser_name, profile_name), btn in self._apply_backup_buttons.items():
            is_running = (
                self._browser_monitor.is_running(browser_name)
                if self._browser_monitor else False
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
        if self._activity_log_widget:
            self._activity_log_widget.setVisible(False)
        self.adjustSize()

    def _on_activity_log_changed(self, index: int) -> None:
        checked = self._activity_log_select.currentData() if self._activity_log_select else True

        if checked:
            if self._log_signaler is None:
                self._log_signaler = LogSignaler()
                self._log_signaler.log_message.connect(self._append_log)
            if self._log_handler is None:
                self._log_handler = GUILogHandler(self._log_signaler)
                self._log_handler.setLevel(logging.DEBUG)
                logging.getLogger().addHandler(self._log_handler)
                _LOG.info("Activity log enabled in settings window")
            if self._activity_log_widget and self._activity_log_text:
                has_logs = self._activity_log_text.toPlainText().strip() != ""
                self._activity_log_widget.setVisible(has_logs)
        else:
            if self._log_handler is not None:
                logging.getLogger().removeHandler(self._log_handler)
                self._log_handler = None
                _LOG.info("Activity log disabled in settings window")
            if self._activity_log_widget:
                self._activity_log_widget.setVisible(False)

        self.adjustSize()

    def _on_autostart_changed(self, index: int) -> None:
        checked = self._autostart_select.currentData() if self._autostart_select else True
        config_module.set_autostart(checked)
        _LOG.info("Autostart %s", "enabled" if checked else "disabled")

    def _append_log(self, level: str, message: str) -> None:
        if self._activity_log_text is None:
            return

        color = LOG_COLORS.get(level, DEFAULT_LOG_COLOR)

        cursor = self._activity_log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        fmt = cursor.charFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)

        cursor.insertText(message + "\n")

        self._activity_log_text.setTextCursor(cursor)
        self._activity_log_text.ensureCursorVisible()

        if self._activity_log_widget and not self._activity_log_widget.isVisible():
            self._activity_log_widget.setVisible(True)
            self.adjustSize()
        if self._clear_log_btn and not self._clear_log_btn.isVisible():
            self._clear_log_btn.setVisible(True)

    def _clear_activity_log(self) -> None:
        if self._activity_log_text:
            _LOG.info("Activity log cleared")
            self._activity_log_text.clear()
        if self._clear_log_btn:
            self._clear_log_btn.setVisible(False)
        if self._activity_log_widget:
            self._activity_log_widget.setVisible(False)
            self.adjustSize()

    def _pick_initial_upload_profile(self) -> tuple[str, str] | None:
        options: list[tuple[str, str, str]] = []
        for browser in self._browsers:
            for profile_path in browser.discover_profiles():
                friendly = browser.get_profile_name(profile_path)
                display = f"{browser.name} — {friendly}"
                options.append((display, browser.name, profile_path.name))

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

    def _do_initial_upload(self, folder: Path, initial: tuple[str, str]) -> None:
        browser_name, profile_name = initial

        profile_path: Path | None = None
        for browser in self._browsers:
            if browser.name == browser_name:
                for p in browser.discover_profiles():
                    if p.name == profile_name:
                        profile_path = p
                        break
                break

        if profile_path is None:
            self._rebuild_profiles(folder)
            return

        dlg = QDialog(self)
        dlg.setWindowTitle("Uploading Profile")
        dlg.setMinimumWidth(420)
        dlg.setWindowFlags(dlg.windowFlags() & ~0x00000008)
        dlg_layout = QVBoxLayout(dlg)

        op_label = QLabel("Starting…")
        op_label.setWordWrap(True)
        dlg_layout.addWidget(op_label)

        bar = QProgressBar()
        bar.setRange(0, 0)
        dlg_layout.addWidget(bar)

        stats_label = QLabel("")
        dlg_layout.addWidget(stats_label)

        dlg.show()

        start_time = time.monotonic()

        self._upload_worker = _InitialUploadWorker(profile_path, folder)
        self._upload_count = 0

        def _on_step(description: str) -> None:
            self._upload_count += 1
            elapsed = time.monotonic() - start_time
            op_label.setText(f"Copying: <b>{description}</b>")
            rate = self._upload_count / elapsed if elapsed > 0.1 else 0
            stats_label.setText(
                f"{self._upload_count} items copied • {elapsed:.0f}s elapsed"
                + (f" • ~{rate:.1f} items/s" if rate > 0 else "")
            )

        def _on_done() -> None:
            elapsed = time.monotonic() - start_time
            dlg.close()
            config_module.set_sync_folder(folder)
            config_module.set_enabled_profiles({browser_name: [profile_name]})
            config_module.set_enabled_browsers({browser_name: True})
            _LOG.info("Initial upload done: %d items in %.1fs", self._upload_count, elapsed)
            if self._clean_btn:
                self._clean_btn.setVisible(_sync_folder_has_data(folder))
            self._rebuild_profiles(folder)
            self.settings_saved.emit()

        self._upload_worker.step.connect(_on_step)
        self._upload_worker.done.connect(_on_done)
        self._upload_worker.start()

    def _add_profile_row(
        self,
        layout: QVBoxLayout,
        browser_name: str,
        profile_name: str,
        folder: Path | None,
        is_running: bool,
        is_enabled: bool,
        prefix_widgets: list,
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

        def _on_toggle_clicked(bn: str = browser_name, pn: str = profile_name,
                               btn: QPushButton = sync_toggle_btn) -> None:
            enabled = config_module.is_profile_sync_enabled(bn, pn)
            config_module.set_profile_sync_enabled(bn, pn, not enabled)
            btn.setText("Auto-sync: ON" if not enabled else "Auto-sync: OFF")

        sync_toggle_btn.clicked.connect(_on_toggle_clicked)

        def _on_apply_clicked(bn: str = browser_name, pn: str = profile_name,
                              btn: QPushButton = apply_btn,
                              s_btn: QPushButton = sync_toggle_btn,
                              f: Path | None = folder) -> None:
            currently = self._profile_states[bn][pn]
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
                if self._browser_monitor else browser.is_running()
            )

            if len(profiles) == 1:
                profile_name = profiles[0].name
                is_enabled = profile_name in browser_saved
                self._profile_states[browser.name][profile_name] = is_enabled

                indicator = _make_status_indicator(is_running)
                self._browser_status_indicators[browser.name] = indicator
                self._add_profile_row(
                    layout, browser.name, profile_name, folder, is_running, is_enabled,
                    prefix_widgets=[indicator, QLabel(f"<b>{browser.name}</b>")],
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
                        layout, browser.name, profile_name, folder, is_running, is_enabled,
                        prefix_widgets=[spacer, QLabel(f"• {display}")],
                    )

        if not found_any:
            layout.addWidget(QLabel("No supported browsers detected."))

        layout.addStretch()

        if self._profiles_group:
            self._profiles_group.setVisible(True)
        if self._selects_row:
            self._selects_row.setVisible(True)
        if self._activity_log_select:
            if self._activity_log_select.currentData():
                if self._log_signaler is None:
                    self._log_signaler = LogSignaler()
                    self._log_signaler.log_message.connect(self._append_log)
                if self._log_handler is None:
                    self._log_handler = GUILogHandler(self._log_signaler)
                    self._log_handler.setLevel(logging.DEBUG)
                    logging.getLogger().addHandler(self._log_handler)

        self.adjustSize()

    def _save_profiles_config(self) -> None:
        enabled_profiles: dict[str, list[str]] = {
            bn: [pn for pn, on in pm.items() if on]
            for bn, pm in self._profile_states.items()
        }
        enabled_browsers: dict[str, bool] = {
            bn: any(pm.values()) for bn, pm in self._profile_states.items()
        }
        data = config_module.load()
        data["enabled_profiles"] = enabled_profiles
        data["enabled_browsers"] = enabled_browsers
        config_module.save(data)
        _LOG.info("Profile configuration updated")

    def update_profile_progress(
        self, browser: str, profile: str, direction: str, count: int, elapsed: float
    ) -> None:
        key = (browser, profile)
        if key not in self._profile_progress:
            return

        progress_bar, info_label = self._profile_progress[key]
        progress_bar.setRange(0, 0)  # indeterminate
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

        import shutil

        from src.sync_engine import clean_external_extensions
        clean_external_extensions(ALL_BROWSERS)

        for path in ["current", "current.tar"]:
            target = folder / path
            try:
                if target.is_dir():
                    shutil.rmtree(target)
                    _LOG.info("Deleted directory: %s", target)
                elif target.is_file():
                    target.unlink()
                    _LOG.info("Deleted file: %s", target)
            except OSError:
                _LOG.exception("Failed to delete: %s", target)

        config_module.set_enabled_profiles({})
        config_module.set_enabled_browsers({})
        _LOG.info("Sync folder cleaned: %s", folder)

        if self._folder_edit:
            self._folder_edit.textChanged.emit(folder_text)

    def _open_shortcuts_editor(self) -> None:
        import shutil
        import tempfile

        from PySide6.QtWidgets import QDialog, QMessageBox

        from src.sync.archive import pack_to_archive, unpack_archive

        sync_folder = config_module.get_sync_folder()
        if not sync_folder or not sync_folder.exists():
            QMessageBox.warning(self, "No Sync Folder", "Please configure a sync folder first.")
            return

        archive = sync_folder / "current.tar"
        if not archive.exists():
            QMessageBox.information(
                self, "No Backup",
                "No backup archive found.\n\nRun a sync first to create the backup.",
            )
            return

        work_dir = Path(tempfile.mkdtemp(prefix="cps-edit-"))
        try:
            unpack_archive(archive, work_dir)

            shortcuts_json_path = work_dir / "search_shortcuts.json"
            if not shortcuts_json_path.exists():
                QMessageBox.information(
                    self, "No Shortcuts Yet",
                    "Search shortcuts haven't been extracted yet.\n\n"
                    "They will be created on the next sync.",
                )
                return

            editor = ShortcutsEditorDialog(self, shortcuts_json_path=shortcuts_json_path)
            if editor.exec() == QDialog.DialogCode.Accepted:
                pack_to_archive(work_dir, archive)
        finally:
            shutil.rmtree(work_dir, ignore_errors=True)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        if self._browser_monitor is not None:
            try:
                self._browser_monitor.state_changed.disconnect(self._on_browser_state_changed)
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

        if self._autostart_select is not None:
            autostart = config_module.get_autostart()
            self._autostart_select.setCurrentIndex(0 if autostart else 1)
