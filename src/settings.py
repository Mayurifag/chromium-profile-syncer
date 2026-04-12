from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer, Signal
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

_LOG = logging.getLogger(__name__)


def _make_status_indicator(is_running: bool) -> QLabel:
    label = QLabel()

    if is_running:
        pixmap = QPixmap(12, 12)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        painter.setBrush(QColor(80, 200, 120, 60))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, 12, 12)

        painter.setBrush(QColor(80, 200, 120))
        painter.drawEllipse(2, 2, 8, 8)
        painter.end()

        label.setPixmap(pixmap)
        label.setToolTip("Browser is running (quit with Cmd+Q to allow sync)")
    else:
        label.setFixedWidth(12)
        label.setToolTip("")

    return label


def _sync_folder_has_data(folder: Path) -> bool:
    current = folder / "current"
    if not current.is_dir():
        return False
    return any(entry.is_dir() for entry in current.iterdir())


def _sync_folder_is_broken(folder: Path) -> bool:
    metadata = folder / "metadata.json"
    current = folder / "current"
    return metadata.exists() and not current.is_dir()


def _profiles_in_sync_folder(folder: Path) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    current = folder / "current"
    if not current.is_dir():
        return result
    for browser_dir in current.iterdir():
        if browser_dir.is_dir():
            result[browser_dir.name] = {p.name for p in browser_dir.iterdir() if p.is_dir()}
    return result


class SettingsDialog(QDialog):
    settings_saved = Signal()
    sync_requested = Signal()
    sync_interval_changed = Signal(int)  # minutes

    def __init__(self, parent=None, *, browsers_list: list | None = None):
        super().__init__(parent)
        self.setWindowTitle("Chromium Profile Syncer — Settings")
        self.setMinimumWidth(400)
        self.setSizeGripEnabled(False)

        self._browsers = (
            browsers_list if browsers_list is not None
            else [b for b in ALL_BROWSERS if b.is_installed()]
        )

        # {browser_name: {profile_name: bool}}
        self._profile_states: dict[str, dict[str, bool]] = {}
        # {(browser_name, profile_name): (progress_bar, info_label)}
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
        self._sync_interval_combo: QComboBox | None = None
        self._next_sync_label: QLabel | None = None
        self._next_sync_timer: QTimer | None = None
        self._browser_status_indicators: dict[str, QLabel] = {}
        self._browser_buttons: dict[tuple[str, str], QPushButton] = {}
        self._browser_status_timer: QTimer | None = None

        self._build_ui()
        self._load_current_settings()
        self._setup_browser_status_timer()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(8, 8, 8, 8)

        # Sync folder
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

        sync_interval_group = QGroupBox("Automatic Sync")
        sync_interval_layout = QVBoxLayout(sync_interval_group)

        interval_row = QWidget()
        interval_row_layout = QHBoxLayout(interval_row)
        interval_row_layout.setContentsMargins(0, 0, 0, 0)
        interval_row_layout.setSpacing(6)

        trigger_sync_btn = QPushButton("Sync now")
        trigger_sync_btn.clicked.connect(lambda: self.sync_requested.emit())
        interval_row_layout.addWidget(trigger_sync_btn)

        interval_row_layout.addWidget(QLabel("Sync every:"))
        self._sync_interval_combo = QComboBox()
        self._sync_interval_combo.addItem("1 minute", 1)
        self._sync_interval_combo.addItem("5 minutes", 5)
        self._sync_interval_combo.addItem("10 minutes", 10)
        self._sync_interval_combo.addItem("15 minutes", 15)
        self._sync_interval_combo.addItem("30 minutes", 30)
        self._sync_interval_combo.addItem("1 hour", 60)
        self._sync_interval_combo.currentIndexChanged.connect(self._on_sync_interval_changed)
        interval_row_layout.addWidget(self._sync_interval_combo)

        self._next_sync_label = QLabel("Next in: calculating...")
        self._next_sync_label.setStyleSheet(SMALL_MUTED)
        interval_row_layout.addWidget(self._next_sync_label)

        interval_row_layout.addStretch()

        sync_interval_layout.addWidget(interval_row)

        sync_interval_group.setVisible(False)
        root.addWidget(sync_interval_group)
        self._sync_interval_group = sync_interval_group

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

    def _on_folder_changed(self, text: str) -> None:
        folder_text = text.strip()
        if not folder_text:
            self._hide_profiles()
            if self._clean_btn:
                self._clean_btn.setVisible(False)
            return
        folder = Path(folder_text)
        if not folder.is_dir():
            self._hide_profiles()
            if self._clean_btn:
                self._clean_btn.setVisible(False)
            return

        # Check for broken state
        if _sync_folder_is_broken(folder):
            from PySide6.QtWidgets import QMessageBox
            reply = QMessageBox.warning(
                self,
                "Broken Sync Folder",
                f"Sync folder is in broken state:\n{folder}\n\n"
                "Found metadata.json but no current/ folder.\n"
                "Clean and start fresh?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if reply == QMessageBox.StandardButton.Yes:
                self._clean_sync_folder(skip_confirmation=True)
            else:
                self._hide_profiles()
                if self._clean_btn:
                    self._clean_btn.setVisible(False)
            return

        config_module.set_sync_folder(folder)
        self.settings_saved.emit()

        has_data = _sync_folder_has_data(folder)
        if self._clean_btn:
            self._clean_btn.setVisible(has_data)
        if not has_data:
            initial = self._pick_initial_upload_profile()
            if initial is None:
                self._hide_profiles()
                return
            self._do_initial_upload(folder, initial)
        else:
            self._rebuild_profiles(folder)

    def _hide_profiles(self) -> None:
        if self._profiles_group:
            self._profiles_group.setVisible(False)
        if hasattr(self, "_selects_row"):
            self._selects_row.setVisible(False)
        if hasattr(self, "_sync_interval_group"):
            self._sync_interval_group.setVisible(False)
        if self._activity_log_widget:
            self._activity_log_widget.setVisible(False)
        self.adjustSize()

    def _setup_browser_status_timer(self) -> None:
        self._browser_status_timer = QTimer(self)
        self._browser_status_timer.setInterval(2000)
        self._browser_status_timer.timeout.connect(self._update_browser_status_indicators)
        self._browser_status_timer.start()

    def _update_browser_status_indicators(self) -> None:
        for browser in self._browsers:
            indicator = self._browser_status_indicators.get(browser.name)
            if indicator is None:
                continue

            is_running = browser.is_running()

            pixmap = QPixmap(12, 12)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)

            if is_running:
                painter.setBrush(QColor(*RUNNING_GLOW))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(0, 0, 12, 12)

                painter.setBrush(QColor(*RUNNING_DOT))
                painter.drawEllipse(2, 2, 8, 8)
                indicator.setToolTip("Browser is running (quit with Cmd+Q to allow sync)")
            else:
                painter.setBrush(QColor(*NOT_RUNNING_DOT))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(2, 2, 8, 8)
                indicator.setToolTip("Browser is not running")

            painter.end()
            indicator.setPixmap(pixmap)

            for (btn_browser, btn_profile), btn in self._browser_buttons.items():
                if btn_browser == browser.name:
                    btn.setEnabled(not is_running)
                    if is_running:
                        btn.setToolTip("Quit browser first (Cmd+Q)")
                    else:
                        btn.setToolTip("")

    def _on_sync_interval_changed(self) -> None:
        if self._sync_interval_combo is None:
            return
        minutes = self._sync_interval_combo.currentData()
        config_module.set_sync_interval(minutes)
        self.sync_interval_changed.emit(minutes)
        _LOG.info("Sync interval changed to %d minutes", minutes)

    def update_next_sync_time(self, next_sync_seconds: int) -> None:
        if self._next_sync_label is None:
            return

        if next_sync_seconds <= 0:
            self._next_sync_label.setText("Next in: now")
            return

        minutes = next_sync_seconds // 60
        seconds = next_sync_seconds % 60

        if minutes > 0:
            self._next_sync_label.setText(f"Next in: {minutes}m {seconds}s")
        else:
            self._next_sync_label.setText(f"Next in: {seconds}s")

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
        _LOG.info(f"Autostart {'enabled' if checked else 'disabled'}")

    def _append_log(self, level: str, message: str) -> None:
        if self._activity_log_text is None:
            return

        color = LOG_COLORS.get(level, DEFAULT_LOG_COLOR)

        # Insert colored text
        cursor = self._activity_log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        # Apply color
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

        sync_profile_path = folder / "current" / browser_name / profile_name

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

        class _Worker(QThread):
            step = Signal(str)
            done = Signal()

            def __init__(self, src: Path, dst: Path) -> None:
                super().__init__()
                self._src = src
                self._dst = dst

            def run(self) -> None:
                from src.sync_engine import SyncEngine
                engine = SyncEngine(folder)
                engine.sync_browser_profile(
                    self._src, self._dst, direction="push",
                    on_progress=lambda desc: self.step.emit(desc),
                )
                engine.update_metadata()
                self.done.emit()

        self._upload_worker = _Worker(profile_path, sync_profile_path)
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
            # Save the uploaded profile to config
            config_module.set_enabled_profiles({browser_name: [profile_name]})
            config_module.set_enabled_browsers({browser_name: True})
            _LOG.info("Initial upload done: %d items in %.1fs", self._upload_count, elapsed)
            self._rebuild_profiles(folder)

        self._upload_worker.step.connect(_on_step)
        self._upload_worker.done.connect(_on_done)
        self._upload_worker.start()

    def _rebuild_profiles(self, folder: Path | None) -> None:
        layout = self._profiles_scroll_layout
        while layout.count():
            item = layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._profile_states.clear()
        self._profile_progress.clear()
        self._browser_status_indicators.clear()
        self._browser_buttons.clear()

        saved_profiles = config_module.get_enabled_profiles()

        found_any = False

        for browser in self._browsers:
            profiles = browser.discover_profiles()
            if not profiles:
                continue
            found_any = True

            self._profile_states[browser.name] = {}
            browser_saved = set(saved_profiles.get(browser.name, []))

            if len(profiles) == 1:
                profile_path = profiles[0]
                profile_name = profile_path.name
                is_enabled = profile_name in browser_saved
                self._profile_states[browser.name][profile_name] = is_enabled

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

                is_running = browser.is_running()
                indicator = _make_status_indicator(is_running)
                self._browser_status_indicators[browser.name] = indicator
                top_layout.addWidget(indicator)

                name_lbl = QLabel(f"<b>{browser.name}</b>")
                btn = QPushButton("Stop manage" if is_enabled else "Manage")
                btn.setFixedWidth(100)
                if is_running:
                    btn.setEnabled(False)
                    btn.setToolTip("Quit browser first (Cmd+Q)")

                self._browser_buttons[(browser.name, profile_name)] = btn

                def _make_handler(bn: str, pn: str, b: QPushButton) -> None:
                    def _clicked() -> None:
                        currently = self._profile_states[bn][pn]
                        self._profile_states[bn][pn] = not currently
                        b.setText("Stop manage" if not currently else "Manage")

                        # If enabling profile, check if backup exists and mark for restore
                        if not currently and folder is not None:
                            backup_path = folder / "current" / bn / pn
                            if backup_path.exists():
                                config_module.mark_profile_for_restore(bn, pn)
                                _LOG.info("Profile %s/%s marked for initial restore", bn, pn)

                        self._save_profiles_config()
                        if not currently:
                            self.sync_requested.emit()
                    b.clicked.connect(_clicked)

                _make_handler(browser.name, profile_name, btn)

                top_layout.addWidget(name_lbl)
                top_layout.addStretch()
                top_layout.addWidget(btn)

                progress_bar = QProgressBar()
                progress_bar.setMaximumHeight(8)
                progress_bar.setTextVisible(False)
                progress_bar.setVisible(False)

                info_label = QLabel()
                info_label.setStyleSheet(SMALL_MUTED)
                info_label.setVisible(False)

                self._profile_progress[(browser.name, profile_name)] = (progress_bar, info_label)

                row_layout.addWidget(top_row)
                row_layout.addWidget(progress_bar)
                row_layout.addWidget(info_label)

                layout.addWidget(row)
            else:
                is_running = browser.is_running()

                header_row = QWidget()
                header_row.setObjectName("profile_row")
                header_row.setStyleSheet(PROFILE_ROW_STYLE)
                header_layout = QHBoxLayout(header_row)
                header_layout.setContentsMargins(0, 0, 0, 0)
                header_layout.setSpacing(4)

                indicator = _make_status_indicator(is_running)
                self._browser_status_indicators[browser.name] = indicator
                header_layout.addWidget(indicator)

                header_lbl = QLabel(f"<b>{browser.name}</b>")
                header_layout.addWidget(header_lbl)
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

                    spacer = QLabel()
                    spacer.setFixedWidth(12)
                    top_layout.addWidget(spacer)

                    name_lbl = QLabel(f"• {display}")
                    btn = QPushButton("Stop manage" if is_enabled else "Manage")
                    btn.setFixedWidth(100)
                    if is_running:
                        btn.setEnabled(False)
                        btn.setToolTip("Quit browser first (Cmd+Q)")

                    self._browser_buttons[(browser.name, profile_name)] = btn

                    def _make_handler(bn: str, pn: str, b: QPushButton) -> None:
                        def _clicked() -> None:
                            currently = self._profile_states[bn][pn]
                            self._profile_states[bn][pn] = not currently
                            b.setText("Stop manage" if not currently else "Manage")
                            self._save_profiles_config()
                            if not currently:
                                self.sync_requested.emit()
                        b.clicked.connect(_clicked)

                    _make_handler(browser.name, profile_name, btn)

                    top_layout.addWidget(name_lbl)
                    top_layout.addStretch()
                    top_layout.addWidget(btn)

                    progress_bar = QProgressBar()
                    progress_bar.setMaximumHeight(8)
                    progress_bar.setTextVisible(False)
                    progress_bar.setVisible(False)

                    info_label = QLabel()
                    info_label.setStyleSheet(SMALL_MUTED)
                    info_label.setVisible(False)

                    self._profile_progress[(browser.name, profile_name)] = (
                        progress_bar,
                        info_label,
                    )

                    row_layout.addWidget(top_row)
                    row_layout.addWidget(progress_bar)
                    row_layout.addWidget(info_label)

                    layout.addWidget(row)

        if not found_any:
            layout.addWidget(QLabel("No supported browsers detected."))

        layout.addStretch()

        if self._profiles_group:
            self._profiles_group.setVisible(True)
        if hasattr(self, "_selects_row"):
            self._selects_row.setVisible(True)
        if hasattr(self, "_sync_interval_group"):
            self._sync_interval_group.setVisible(True)
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

        for path in ["current", "backup-1", "backup-2", "metadata.json"]:
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
        from PySide6.QtWidgets import QMessageBox

        sync_folder = config_module.get_sync_folder()
        if not sync_folder or not sync_folder.exists():
            QMessageBox.warning(
                self,
                "No Sync Folder",
                "Please configure a sync folder first.",
            )
            return

        shortcuts_json_path = sync_folder / "search_shortcuts.json"
        if not shortcuts_json_path.exists():
            has_data = _sync_folder_has_data(sync_folder)

            if has_data:
                reply = QMessageBox.warning(
                    self,
                    "Corrupted Backup",
                    "Search shortcuts backup file is missing.\n\n"
                    "This indicates the backup is corrupted.\n"
                    "Clean the sync folder and start fresh?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes,
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self._clean_sync_folder(skip_confirmation=True)
            else:
                QMessageBox.information(
                    self,
                    "No Shortcuts Yet",
                    "Search shortcuts haven't been extracted yet.\n\n"
                    "They will be created on the next sync.",
                )
            return

        editor = ShortcutsEditorDialog(self, shortcuts_json_path=shortcuts_json_path)
        editor.exec()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None
        if self._next_sync_timer is not None:
            self._next_sync_timer.stop()
            self._next_sync_timer = None
        if self._browser_status_timer is not None:
            self._browser_status_timer.stop()
            self._browser_status_timer = None
        super().closeEvent(event)

    def _load_current_settings(self) -> None:
        if self._sync_interval_combo is not None:
            interval = config_module.get_sync_interval()
            for i in range(self._sync_interval_combo.count()):
                if self._sync_interval_combo.itemData(i) == interval:
                    self._sync_interval_combo.setCurrentIndex(i)
                    break

        sync_folder = config_module.get_sync_folder()
        if sync_folder and self._folder_edit:
            self._folder_edit.blockSignals(True)
            self._folder_edit.setText(str(sync_folder))
            self._folder_edit.blockSignals(False)
            if sync_folder.is_dir():
                if _sync_folder_is_broken(sync_folder):
                    from PySide6.QtWidgets import QMessageBox
                    reply = QMessageBox.warning(
                        self,
                        "Broken Sync Folder",
                        f"Sync folder is in broken state:\n{sync_folder}\n\n"
                        "Found metadata.json but no current/ folder.\n"
                        "Clean and start fresh?",
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.Yes,
                    )
                    if reply == QMessageBox.StandardButton.Yes:
                        self._clean_sync_folder(skip_confirmation=True)
                    else:
                        self._hide_profiles()
                        if self._clean_btn:
                            self._clean_btn.setVisible(False)
                else:
                    has_data = _sync_folder_has_data(sync_folder)
                    if self._clean_btn:
                        self._clean_btn.setVisible(has_data)
                    self._rebuild_profiles(sync_folder)
            else:
                self._hide_profiles()
        else:
            self._hide_profiles()

        if self._autostart_select is not None:
            autostart = config_module.get_autostart()
            self._autostart_select.setCurrentIndex(0 if autostart else 1)
