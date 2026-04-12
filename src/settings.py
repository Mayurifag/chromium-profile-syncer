from __future__ import annotations

import logging
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtGui import QColor, QPalette
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
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import src.config as config_module
from src.browsers import ALL_BROWSERS
from src.browsers.base import BrowserBase

_LOG = logging.getLogger(__name__)


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
        self._autostart_check: QCheckBox | None = None
        self._folder_edit: QLineEdit | None = None
        self._clean_btn: QPushButton | None = None
        self._profiles_group: QGroupBox | None = None
        self._scroll_area: QScrollArea | None = None
        self._profiles_scroll_layout: QVBoxLayout | None = None

        self._build_ui()
        self._load_current_settings()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # Sync folder
        folder_group = QGroupBox("Sync folder")
        folder_layout = QHBoxLayout(folder_group)
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText("Select a folder…")
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
        self._profiles_group.setMinimumHeight(150)
        profiles_group_layout = QVBoxLayout(self._profiles_group)

        self._scroll_area = QScrollArea()
        self._scroll_area.setWidgetResizable(True)

        scroll_content = QWidget()
        self._profiles_scroll_layout = QVBoxLayout(scroll_content)
        self._profiles_scroll_layout.setSpacing(4)
        scroll_content.setLayout(self._profiles_scroll_layout)
        self._scroll_area.setWidget(scroll_content)
        profiles_group_layout.addWidget(self._scroll_area)

        root.addWidget(self._profiles_group)

        # Autostart (hidden until folder is set)
        self._autostart_check = QCheckBox("Launch on login")
        self._autostart_check.setChecked(True)
        self._autostart_check.setVisible(False)
        root.addWidget(self._autostart_check)

        # OK / Cancel — only applies to folder + autostart
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

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
        if self._autostart_check:
            self._autostart_check.setVisible(False)
        # Adjust window size to fit content
        self.adjustSize()

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

        synced_profiles = _profiles_in_sync_folder(folder) if folder is not None else {}
        saved_profiles = config_module.get_enabled_profiles()

        found_any = False
        row_index = 0  # for zebra striping

        for browser in self._browsers:
            profiles = browser.discover_profiles()
            if not profiles:
                continue
            found_any = True

            self._profile_states[browser.name] = {}
            browser_synced = synced_profiles.get(browser.name, set())
            browser_saved = set(saved_profiles.get(browser.name, []))

            if len(profiles) == 1:
                # Single profile: show browser name + sync button
                profile_path = profiles[0]
                profile_name = profile_path.name
                is_enabled = profile_name in browser_synced or profile_name in browser_saved
                self._profile_states[browser.name][profile_name] = is_enabled

                row = QWidget()
                row_layout = QVBoxLayout(row)
                row_layout.setContentsMargins(4, 4, 4, 4)
                row_layout.setSpacing(2)

                # Top row: name + button
                top_row = QWidget()
                top_layout = QHBoxLayout(top_row)
                top_layout.setContentsMargins(0, 0, 0, 0)

                name_lbl = QLabel(f"<b>{browser.name}</b>")
                btn = QPushButton("Stop sync" if is_enabled else "Sync")
                btn.setFixedWidth(90)

                def _make_handler(bn: str, pn: str, b: QPushButton) -> None:
                    def _clicked() -> None:
                        currently = self._profile_states[bn][pn]
                        self._profile_states[bn][pn] = not currently
                        b.setText("Stop sync" if not currently else "Sync")
                        self._save_profiles_config()
                        self.settings_saved.emit()
                        if not currently:  # just enabled → sync now
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
                info_label.setStyleSheet("font-size: 10px; color: #888;")
                info_label.setVisible(False)

                self._profile_progress[(browser.name, profile_name)] = (progress_bar, info_label)

                row_layout.addWidget(top_row)
                row_layout.addWidget(progress_bar)
                row_layout.addWidget(info_label)

                # Zebra striping using palette (doesn't affect button styling)
                row.setAutoFillBackground(True)
                palette = row.palette()
                bg_color = QColor("#2a2a2a") if row_index % 2 == 0 else QColor("#242424")
                palette.setColor(QPalette.ColorRole.Window, bg_color)
                row.setPalette(palette)
                row_index += 1

                layout.addWidget(row)
            else:
                # Multiple profiles: show browser header + profile rows
                layout.addWidget(QLabel(f"<b>{browser.name}</b>"))

                for profile_path in profiles:
                    profile_name = profile_path.name
                    friendly = BrowserBase.get_profile_name(profile_path)
                    display = (
                        f"{friendly}  ({profile_name})"
                        if friendly != profile_name
                        else profile_name
                    )
                    is_enabled = profile_name in browser_synced or profile_name in browser_saved
                    self._profile_states[browser.name][profile_name] = is_enabled

                    row = QWidget()
                    row_layout = QVBoxLayout(row)
                    row_layout.setContentsMargins(16, 4, 4, 4)
                    row_layout.setSpacing(2)

                    # Top row: name + button
                    top_row = QWidget()
                    top_layout = QHBoxLayout(top_row)
                    top_layout.setContentsMargins(0, 0, 0, 0)

                    name_lbl = QLabel(display)
                    btn = QPushButton("Stop sync" if is_enabled else "Sync")
                    btn.setFixedWidth(90)

                    def _make_handler(bn: str, pn: str, b: QPushButton) -> None:
                        def _clicked() -> None:
                            currently = self._profile_states[bn][pn]
                            self._profile_states[bn][pn] = not currently
                            b.setText("Stop sync" if not currently else "Sync")
                            self._save_profiles_config()
                            self.settings_saved.emit()
                            if not currently:  # just enabled → sync now
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
                    info_label.setStyleSheet("font-size: 10px; color: #888;")
                    info_label.setVisible(False)

                    self._profile_progress[(browser.name, profile_name)] = (
                        progress_bar,
                        info_label,
                    )

                    row_layout.addWidget(top_row)
                    row_layout.addWidget(progress_bar)
                    row_layout.addWidget(info_label)

                    # Zebra striping using palette (doesn't affect button styling)
                    row.setAutoFillBackground(True)
                    palette = row.palette()
                    bg_color = QColor("#2a2a2a") if row_index % 2 == 0 else QColor("#242424")
                    palette.setColor(QPalette.ColorRole.Window, bg_color)
                    row.setPalette(palette)
                    row_index += 1

                    layout.addWidget(row)

        if not found_any:
            layout.addWidget(QLabel("No supported browsers detected."))

        layout.addStretch()

        if self._profiles_group:
            self._profiles_group.setVisible(True)
        if self._autostart_check:
            self._autostart_check.setVisible(True)

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
        config_module.set_enabled_profiles(enabled_profiles)
        config_module.set_enabled_browsers(enabled_browsers)

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

    def _on_accept(self) -> None:
        folder_text = self._folder_edit.text().strip() if self._folder_edit else ""
        if folder_text:
            config_module.set_sync_folder(Path(folder_text))

        if self._autostart_check is not None:
            config_module.set_autostart(self._autostart_check.isChecked())

        _LOG.info("Settings saved")
        self.settings_saved.emit()
        self.accept()

    # ------------------------------------------------------------------
    # Load helpers
    # ------------------------------------------------------------------

    def _load_current_settings(self) -> None:
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

        if self._autostart_check is not None:
            self._autostart_check.setChecked(config_module.get_autostart())
