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
from src.browsers.base import BrowserBase
from src.log_viewer import GUILogHandler, LogSignaler

_LOG = logging.getLogger(__name__)


def _make_status_indicator(is_running: bool) -> QLabel:
    """Create an elegant status indicator with tooltip."""
    label = QLabel()

    if is_running:
        # Create a small green dot icon
        pixmap = QPixmap(12, 12)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Draw a subtle glow
        painter.setBrush(QColor(80, 200, 120, 60))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(0, 0, 12, 12)

        # Draw the main dot
        painter.setBrush(QColor(80, 200, 120))
        painter.drawEllipse(2, 2, 8, 8)
        painter.end()

        label.setPixmap(pixmap)
        label.setToolTip("Browser is running (quit with Cmd+Q to allow sync)")
    else:
        # Empty space to maintain alignment
        label.setFixedWidth(12)
        label.setToolTip("")

    return label


def _sync_folder_has_data(folder: Path) -> bool:
    current = folder / "current"
    if not current.is_dir():
        return False
    return any(entry.is_dir() for entry in current.iterdir())


def _sync_folder_is_broken(folder: Path) -> bool:
    """Check if sync folder is in broken state (metadata exists but no current/ folder)."""
    metadata = folder / "metadata.json"
    current = folder / "current"
    return metadata.exists() and not current.is_dir()


def _profiles_in_sync_folder(folder: Path) -> dict[str, set[str]]:
    """Return {browser_name: {profile_name}} for profiles present in sync folder."""
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
        self.setMinimumWidth(520)
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
        self._log_signaler: LogSignaler | None = None
        self._log_handler: GUILogHandler | None = None
        self._sync_interval_combo: QComboBox | None = None
        self._next_sync_label: QLabel | None = None
        self._next_sync_timer: QTimer | None = None
        self._browser_status_indicators: dict[str, QLabel] = {}  # browser_name -> indicator
        # (browser_name, profile_name) -> button
        self._browser_buttons: dict[tuple[str, str], QPushButton] = {}
        self._browser_status_timer: QTimer | None = None

        self._build_ui()
        self._load_current_settings()
        self._setup_browser_status_timer()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(12, 12, 12, 12)

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

        # Profiles section (hidden until folder is set)
        self._profiles_group = QGroupBox("Profiles")
        self._profiles_group.setVisible(False)
        self._profiles_scroll_layout = QVBoxLayout(self._profiles_group)
        self._profiles_scroll_layout.setSpacing(4)

        root.addWidget(self._profiles_group)

        # Sync interval selector (hidden until folder is set)
        sync_interval_group = QGroupBox("Automatic Sync")
        sync_interval_layout = QVBoxLayout(sync_interval_group)

        interval_row = QWidget()
        interval_row_layout = QHBoxLayout(interval_row)
        interval_row_layout.setContentsMargins(0, 0, 0, 0)

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

        # Trigger sync now button on the same row
        trigger_sync_btn = QPushButton("Trigger sync now")
        trigger_sync_btn.clicked.connect(lambda: self.sync_requested.emit())
        interval_row_layout.addWidget(trigger_sync_btn)

        interval_row_layout.addStretch()

        sync_interval_layout.addWidget(interval_row)

        self._next_sync_label = QLabel("Next sync: calculating...")
        self._next_sync_label.setStyleSheet("color: #6272a4; font-size: 11px;")
        sync_interval_layout.addWidget(self._next_sync_label)

        sync_interval_group.setVisible(False)
        root.addWidget(sync_interval_group)
        self._sync_interval_group = sync_interval_group

        # Activity log and autostart selects (hidden until folder is set)
        selects_row = QWidget()
        selects_layout = QHBoxLayout(selects_row)
        selects_layout.setContentsMargins(0, 0, 0, 0)
        selects_layout.setSpacing(12)

        selects_layout.addWidget(QLabel("Show activity log:"))
        self._activity_log_select = QComboBox()
        self._activity_log_select.addItem("Yes", True)
        self._activity_log_select.addItem("No", False)
        self._activity_log_select.setCurrentIndex(0)
        self._activity_log_select.currentIndexChanged.connect(self._on_activity_log_changed)
        selects_layout.addWidget(self._activity_log_select)

        selects_layout.addSpacing(12)

        selects_layout.addWidget(QLabel("Launch on login:"))
        self._autostart_select = QComboBox()
        self._autostart_select.addItem("Yes", True)
        self._autostart_select.addItem("No", False)
        self._autostart_select.setCurrentIndex(0)
        self._autostart_select.currentIndexChanged.connect(self._on_autostart_changed)
        selects_layout.addWidget(self._autostart_select)

        selects_layout.addStretch()
        selects_row.setVisible(False)
        root.addWidget(selects_row)
        self._selects_row = selects_row

        # Activity log section (hidden by default)
        self._activity_log_widget = QWidget()
        log_layout = QVBoxLayout(self._activity_log_widget)
        log_layout.setContentsMargins(0, 0, 0, 0)

        # Text display
        self._activity_log_text = QTextEdit()
        self._activity_log_text.setReadOnly(True)
        self._activity_log_text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._activity_log_text.setMinimumHeight(200)
        self._activity_log_text.setMaximumHeight(300)

        # Use monospace font for better command display
        font = QFont("Monaco, Menlo, Courier New, monospace")
        font.setPointSize(11)
        self._activity_log_text.setFont(font)

        log_layout.addWidget(self._activity_log_text)

        # Clear button
        clear_btn = QPushButton("Clear Log")
        clear_btn.clicked.connect(self._clear_activity_log)
        log_layout.addWidget(clear_btn)

        self._activity_log_widget.setVisible(False)
        root.addWidget(self._activity_log_widget)

    # ------------------------------------------------------------------
    # Folder change
    # ------------------------------------------------------------------

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

        # Save folder to config immediately after validation
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
        # Adjust window size to fit content
        self.adjustSize()

    # ------------------------------------------------------------------
    # Browser status updates
    # ------------------------------------------------------------------

    def _setup_browser_status_timer(self) -> None:
        """Set up a timer to update browser running indicators every 2 seconds."""
        self._browser_status_timer = QTimer(self)
        self._browser_status_timer.setInterval(2000)  # 2 seconds
        self._browser_status_timer.timeout.connect(self._update_browser_status_indicators)
        self._browser_status_timer.start()

    def _update_browser_status_indicators(self) -> None:
        """Update running indicators and button states for all browsers."""
        for browser in self._browsers:
            indicator = self._browser_status_indicators.get(browser.name)
            if indicator is None:
                continue

            is_running = browser.is_running()

            # Update the indicator pixmap
            if is_running:
                pixmap = QPixmap(12, 12)
                pixmap.fill(Qt.GlobalColor.transparent)
                painter = QPainter(pixmap)
                painter.setRenderHint(QPainter.RenderHint.Antialiasing)

                # Draw a subtle glow
                painter.setBrush(QColor(80, 200, 120, 60))
                painter.setPen(Qt.PenStyle.NoPen)
                painter.drawEllipse(0, 0, 12, 12)

                # Draw the main dot
                painter.setBrush(QColor(80, 200, 120))
                painter.drawEllipse(2, 2, 8, 8)
                painter.end()

                indicator.setPixmap(pixmap)
                indicator.setToolTip("Browser is running (quit with Cmd+Q to allow sync)")
            else:
                indicator.setPixmap(QPixmap())  # Clear pixmap
                indicator.setToolTip("")

            # Update button states for all profiles of this browser
            for (btn_browser, btn_profile), btn in self._browser_buttons.items():
                if btn_browser == browser.name:
                    btn.setEnabled(not is_running)
                    if is_running:
                        btn.setToolTip("Quit browser first (Cmd+Q)")
                    else:
                        btn.setToolTip("")

    # ------------------------------------------------------------------
    # Sync interval
    # ------------------------------------------------------------------

    def _on_sync_interval_changed(self) -> None:
        """Save sync interval when changed."""
        if self._sync_interval_combo is None:
            return
        minutes = self._sync_interval_combo.currentData()
        config_module.set_sync_interval(minutes)
        self.sync_interval_changed.emit(minutes)
        _LOG.info("Sync interval changed to %d minutes", minutes)

    def update_next_sync_time(self, next_sync_seconds: int) -> None:
        """Update the next sync countdown display."""
        if self._next_sync_label is None:
            return

        if next_sync_seconds <= 0:
            self._next_sync_label.setText("Next sync: now")
            return

        minutes = next_sync_seconds // 60
        seconds = next_sync_seconds % 60

        if minutes > 0:
            self._next_sync_label.setText(f"Next sync in: {minutes}m {seconds}s")
        else:
            self._next_sync_label.setText(f"Next sync in: {seconds}s")

    # ------------------------------------------------------------------
    # Activity log
    # ------------------------------------------------------------------

    def _on_activity_log_changed(self, index: int) -> None:
        """Show or hide the activity log section."""
        checked = self._activity_log_select.currentData() if self._activity_log_select else True
        if self._activity_log_widget:
            self._activity_log_widget.setVisible(checked)

        # Set up or tear down logging handler
        if checked:
            if self._log_signaler is None:
                self._log_signaler = LogSignaler()
                self._log_signaler.log_message.connect(self._append_log)
            if self._log_handler is None:
                self._log_handler = GUILogHandler(self._log_signaler)
                self._log_handler.setLevel(logging.DEBUG)
                logging.getLogger().addHandler(self._log_handler)
                _LOG.info("Activity log enabled in settings window")
        else:
            if self._log_handler is not None:
                logging.getLogger().removeHandler(self._log_handler)
                self._log_handler = None
                _LOG.info("Activity log disabled in settings window")

        # Adjust window size to fit content
        self.adjustSize()

    def _on_autostart_changed(self, index: int) -> None:
        """Save autostart setting immediately."""
        checked = self._autostart_select.currentData() if self._autostart_select else True
        config_module.set_autostart(checked)
        _LOG.info(f"Autostart {'enabled' if checked else 'disabled'}")

    def _append_log(self, level: str, message: str) -> None:
        """Append a log message with color coding based on level."""
        if self._activity_log_text is None:
            return

        # Color map for log levels
        colors = {
            "DEBUG": "#808080",     # Gray
            "INFO": "#4ec9b0",      # Teal
            "WARNING": "#dcdcaa",   # Yellow
            "ERROR": "#f48771",     # Red
            "CRITICAL": "#ff0000",  # Bright red
        }
        color = colors.get(level, "#d4d4d4")

        # Insert colored text
        cursor = self._activity_log_text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        # Apply color
        fmt = cursor.charFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)

        # Insert message
        cursor.insertText(message + "\n")

        # Auto-scroll to bottom
        self._activity_log_text.setTextCursor(cursor)
        self._activity_log_text.ensureCursorVisible()

    def _clear_activity_log(self) -> None:
        """Clear all log messages."""
        if self._activity_log_text:
            self._activity_log_text.clear()
            _LOG.info("Activity log cleared")

    # ------------------------------------------------------------------
    # Initial upload
    # ------------------------------------------------------------------

    def _pick_initial_upload_profile(self) -> tuple[str, str] | None:
        options: list[tuple[str, str, str]] = []
        for browser in self._browsers:
            for profile_path in browser.discover_profiles():
                friendly = BrowserBase.get_profile_name(profile_path)
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

    # ------------------------------------------------------------------
    # Profile list
    # ------------------------------------------------------------------

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
                # Single profile: show browser name + sync button
                profile_path = profiles[0]
                profile_name = profile_path.name
                is_enabled = profile_name in browser_saved
                self._profile_states[browser.name][profile_name] = is_enabled

                row = QWidget()
                row_layout = QVBoxLayout(row)
                row_layout.setContentsMargins(4, 4, 4, 4)
                row_layout.setSpacing(2)

                # Top row: name + button
                top_row = QWidget()
                top_layout = QHBoxLayout(top_row)
                top_layout.setContentsMargins(0, 0, 0, 0)
                top_layout.setSpacing(6)

                # Browser name with running indicator
                is_running = browser.is_running()
                indicator = _make_status_indicator(is_running)
                self._browser_status_indicators[browser.name] = indicator
                top_layout.addWidget(indicator)

                name_lbl = QLabel(f"<b>{browser.name}</b>")
                btn = QPushButton("Stop manage" if is_enabled else "Manage")
                btn.setFixedWidth(100)
                # Disable button if browser is running
                if is_running:
                    btn.setEnabled(False)
                    btn.setToolTip("Quit browser first (Cmd+Q)")

                # Store button reference for status updates
                self._browser_buttons[(browser.name, profile_name)] = btn

                def _make_handler(bn: str, pn: str, b: QPushButton) -> None:
                    def _clicked() -> None:
                        currently = self._profile_states[bn][pn]
                        self._profile_states[bn][pn] = not currently
                        b.setText("Stop manage" if not currently else "Manage")
                        self._save_profiles_config()
                        # Trigger sync when enabling a profile
                        if not currently:
                            self.sync_requested.emit()
                    b.clicked.connect(_clicked)

                _make_handler(browser.name, profile_name, btn)

                top_layout.addWidget(name_lbl)
                top_layout.addStretch()
                top_layout.addWidget(btn)

                # Progress section
                progress_bar = QProgressBar()
                progress_bar.setMaximumHeight(8)
                progress_bar.setTextVisible(False)
                progress_bar.setVisible(False)

                info_label = QLabel()
                info_label.setStyleSheet("font-size: 10px; color: #6272a4;")
                info_label.setVisible(False)

                self._profile_progress[(browser.name, profile_name)] = (progress_bar, info_label)

                row_layout.addWidget(top_row)
                row_layout.addWidget(progress_bar)
                row_layout.addWidget(info_label)

                layout.addWidget(row)
            else:
                # Multiple profiles: show browser header + profile rows
                is_running = browser.is_running()

                # Create header row with indicator
                header_row = QWidget()
                header_layout = QHBoxLayout(header_row)
                header_layout.setContentsMargins(4, 4, 4, 4)
                header_layout.setSpacing(6)

                indicator = _make_status_indicator(is_running)
                self._browser_status_indicators[browser.name] = indicator
                header_layout.addWidget(indicator)

                header_lbl = QLabel(f"<b>{browser.name}</b>")
                header_layout.addWidget(header_lbl)
                header_layout.addStretch()

                layout.addWidget(header_row)

                for profile_path in profiles:
                    profile_name = profile_path.name
                    friendly = BrowserBase.get_profile_name(profile_path)
                    display = (
                        f"{friendly}  ({profile_name})"
                        if friendly != profile_name
                        else profile_name
                    )
                    is_enabled = profile_name in browser_saved
                    self._profile_states[browser.name][profile_name] = is_enabled

                    row = QWidget()
                    row_layout = QVBoxLayout(row)
                    row_layout.setContentsMargins(4, 4, 4, 4)
                    row_layout.setSpacing(2)

                    # Top row: name + button
                    top_row = QWidget()
                    top_layout = QHBoxLayout(top_row)
                    top_layout.setContentsMargins(0, 0, 0, 0)
                    top_layout.setSpacing(6)

                    # Add spacing to align with indicator position
                    spacer = QLabel()
                    spacer.setFixedWidth(12)  # Same as indicator width
                    top_layout.addWidget(spacer)

                    name_lbl = QLabel(f"- {display}")
                    btn = QPushButton("Stop manage" if is_enabled else "Manage")
                    btn.setFixedWidth(100)
                    # Disable button if browser is running (use parent browser's running state)
                    if is_running:
                        btn.setEnabled(False)
                        btn.setToolTip("Quit browser first (Cmd+Q)")

                    # Store button reference for status updates
                    self._browser_buttons[(browser.name, profile_name)] = btn

                    def _make_handler(bn: str, pn: str, b: QPushButton) -> None:
                        def _clicked() -> None:
                            currently = self._profile_states[bn][pn]
                            self._profile_states[bn][pn] = not currently
                            b.setText("Stop manage" if not currently else "Manage")
                            self._save_profiles_config()
                            # Trigger sync when enabling a profile
                            if not currently:
                                self.sync_requested.emit()
                        b.clicked.connect(_clicked)

                    _make_handler(browser.name, profile_name, btn)

                    top_layout.addWidget(name_lbl)
                    top_layout.addStretch()
                    top_layout.addWidget(btn)

                    # Progress section
                    progress_bar = QProgressBar()
                    progress_bar.setMaximumHeight(8)
                    progress_bar.setTextVisible(False)
                    progress_bar.setVisible(False)

                    info_label = QLabel()
                    info_label.setStyleSheet("font-size: 10px; color: #6272a4;")
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
            # Trigger activity log to be shown by default
            if self._activity_log_select.currentData() and self._activity_log_widget:
                self._activity_log_widget.setVisible(True)
                # Set up logging handler if not already done
                if self._log_signaler is None:
                    self._log_signaler = LogSignaler()
                    self._log_signaler.log_message.connect(self._append_log)
                if self._log_handler is None:
                    self._log_handler = GUILogHandler(self._log_signaler)
                    self._log_handler.setLevel(logging.DEBUG)
                    logging.getLogger().addHandler(self._log_handler)

        # Adjust window size to fit content
        self.adjustSize()

    def _save_profiles_config(self) -> None:
        enabled_profiles: dict[str, list[str]] = {
            bn: [pn for pn, on in pm.items() if on]
            for bn, pm in self._profile_states.items()
        }
        enabled_browsers: dict[str, bool] = {
            bn: any(pm.values()) for bn, pm in self._profile_states.items()
        }
        # Save both in one operation to avoid duplicate saves
        data = config_module.load()
        data["enabled_profiles"] = enabled_profiles
        data["enabled_browsers"] = enabled_browsers
        config_module.save(data)
        _LOG.info("Profile configuration updated")

    # ------------------------------------------------------------------
    # Progress tracking
    # ------------------------------------------------------------------

    def update_profile_progress(
        self, browser: str, profile: str, direction: str, count: int, elapsed: float
    ) -> None:
        """Update progress for a profile sync."""
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
        """Hide progress for a profile."""
        key = (browser, profile)
        if key not in self._profile_progress:
            return

        progress_bar, info_label = self._profile_progress[key]
        progress_bar.setVisible(False)
        info_label.setVisible(False)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _browse_folder(self) -> None:
        current = self._folder_edit.text() if self._folder_edit else ""
        chosen = QFileDialog.getExistingDirectory(self, "Select sync folder", current)
        if chosen and self._folder_edit:
            self._folder_edit.setText(chosen)

    def _clean_sync_folder(self, *, skip_confirmation: bool = False) -> None:
        """Clean all synced data and start from scratch."""
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

        # Delete current, backup-1, backup-2, and metadata.json
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

        # Clear config
        config_module.set_enabled_profiles({})
        config_module.set_enabled_browsers({})
        _LOG.info("Sync folder cleaned: %s", folder)

        # Trigger folder changed to show initial upload dialog
        if self._folder_edit:
            self._folder_edit.textChanged.emit(folder_text)

    def closeEvent(self, event) -> None:  # noqa: N802
        """Remove logging handler when window closes."""
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

    # ------------------------------------------------------------------
    # Load helpers
    # ------------------------------------------------------------------

    def _load_current_settings(self) -> None:
        # Load sync interval
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
                # Check for broken state first
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
                    # Show Clean button if folder has data
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
