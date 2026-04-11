from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Signal
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
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

import src.config as config_module
from src.browsers import ALL_BROWSERS
from src.browsers.base import BrowserBase

_LOG = logging.getLogger(__name__)

_NOT_SYNCED_TEXT = "Not synced: passwords, cookies, history, payment info, search engines"
_NOT_SYNCED_TOOLTIP = (
    "These are either encrypted with a machine-specific key (passwords, cookies, payment info) "
    "or explicitly excluded (history). Search engine sync requires SQL extraction and is not yet "
    "implemented."
)

_DATA_TYPE_TOOLTIPS: dict[str, str] = {
    "extensions": (
        "Browser extensions (code + settings). Includes ad blockers, themes, password managers, etc."
    ),
    "bookmarks": "Saved bookmarks and folder structure.",
    "custom_dictionary": "Words added to the browser spell-check dictionary.",
    "local_storage": (
        "Data websites store locally — login states, preferences, dark mode toggles, etc."
    ),
    "indexeddb": (
        "Larger structured data stored by websites and extensions — offline caches, app state, etc."
    ),
}

_DIRECTION_OPTIONS: list[tuple[str, str]] = [
    ("↕  Bidirectional", "both"),
    ("↑  Upload to sync folder", "push"),
    ("↓  Restore from sync folder", "pull"),
]


class SettingsDialog(QDialog):
    settings_saved = Signal()

    def __init__(self, parent=None, *, browsers_list: list[BrowserBase] | None = None):
        super().__init__(parent)
        self.setWindowTitle("Chromium Profile Syncer — Settings")
        self.setMinimumWidth(480)

        self._browsers: list[BrowserBase] = (
            browsers_list
            if browsers_list is not None
            else [b for b in ALL_BROWSERS if b.is_installed()]
        )

        self._profile_checks: dict[str, dict[str, QCheckBox]] = {}
        self._profile_directions: dict[str, dict[str, QComboBox]] = {}
        self._data_type_checks: dict[str, QCheckBox] = {}
        self._autostart_check: QCheckBox | None = None
        self._folder_edit: QLineEdit | None = None

        self._build_ui()
        self._load_current_settings()

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        # --- Sync folder ---
        folder_group = QGroupBox("Sync folder")
        folder_layout = QHBoxLayout(folder_group)
        self._folder_edit = QLineEdit()
        self._folder_edit.setPlaceholderText("Select a folder…")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_folder)
        folder_layout.addWidget(self._folder_edit)
        folder_layout.addWidget(browse_btn)
        root.addWidget(folder_group)

        # --- Browsers & profiles ---
        browsers_group = QGroupBox("Browsers && profiles")
        browsers_layout = QVBoxLayout(browsers_group)

        scroll_area = QScrollArea()
        scroll_area.setWidgetResizable(True)
        scroll_content = QWidget()
        scroll_layout = QVBoxLayout(scroll_content)
        scroll_layout.setSpacing(2)

        if self._browsers:
            for browser in self._browsers:
                # Browser name as bold label (no checkbox)
                browser_label = QLabel(f"<b>{browser.name}</b>")
                scroll_layout.addWidget(browser_label)

                profile_checks: dict[str, QCheckBox] = {}
                profile_directions: dict[str, QComboBox] = {}
                for profile_path in browser.discover_profiles():
                    profile_name = profile_path.name
                    friendly = BrowserBase.get_profile_name(profile_path)
                    if friendly != profile_name:
                        display = f"{friendly}  ({profile_name})"
                    else:
                        display = profile_name

                    row_widget = QWidget()
                    row_layout = QHBoxLayout(row_widget)
                    row_layout.setContentsMargins(16, 0, 0, 0)

                    profile_cb = QCheckBox(display)
                    profile_cb.setChecked(True)
                    profile_checks[profile_name] = profile_cb

                    direction_combo = QComboBox()
                    for label, data in _DIRECTION_OPTIONS:
                        direction_combo.addItem(label, data)
                    profile_directions[profile_name] = direction_combo

                    row_layout.addWidget(profile_cb)
                    row_layout.addStretch()
                    row_layout.addWidget(direction_combo)
                    scroll_layout.addWidget(row_widget)

                self._profile_checks[browser.name] = profile_checks
                self._profile_directions[browser.name] = profile_directions
        else:
            scroll_layout.addWidget(QLabel("No supported browsers detected."))

        scroll_content.setLayout(scroll_layout)
        scroll_area.setWidget(scroll_content)
        browsers_layout.addWidget(scroll_area)
        root.addWidget(browsers_group)

        # --- Data types ---
        data_group = QGroupBox("Data types to sync")
        data_layout = QVBoxLayout(data_group)

        syncable = [
            ("extensions", "Extensions"),
            ("bookmarks", "Bookmarks"),
            ("custom_dictionary", "Custom dictionary"),
            ("local_storage", "Local storage"),
            ("indexeddb", "IndexedDB"),
        ]
        for key, label in syncable:
            cb = QCheckBox(label)
            cb.setChecked(True)
            tooltip = _DATA_TYPE_TOOLTIPS.get(key, "")
            if tooltip:
                cb.setToolTip(tooltip)
            self._data_type_checks[key] = cb
            data_layout.addWidget(cb)

        # Not-synced notice
        not_synced_label = QLabel(_NOT_SYNCED_TEXT)
        not_synced_label.setEnabled(False)
        not_synced_label.setToolTip(_NOT_SYNCED_TOOLTIP)
        data_layout.addWidget(not_synced_label)

        root.addWidget(data_group)

        # --- Autostart ---
        self._autostart_check = QCheckBox("Launch at login (autostart)")
        root.addWidget(self._autostart_check)

        # --- Buttons ---
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    # ------------------------------------------------------------------
    # Slots
    # ------------------------------------------------------------------

    def _browse_folder(self) -> None:
        current = self._folder_edit.text() if self._folder_edit else ""
        chosen = QFileDialog.getExistingDirectory(self, "Select sync folder", current)
        if chosen and self._folder_edit:
            self._folder_edit.setText(chosen)

    def _on_accept(self) -> None:
        # Sync folder
        folder_text = self._folder_edit.text().strip() if self._folder_edit else ""
        if folder_text:
            config_module.set_sync_folder(Path(folder_text))

        # Browsers — derived from whether any profile is checked
        enabled_browsers: dict[str, bool] = {}
        for browser_name, profile_map in self._profile_checks.items():
            enabled_browsers[browser_name] = any(cb.isChecked() for cb in profile_map.values())
        config_module.set_enabled_browsers(enabled_browsers)

        # Profiles
        enabled_profiles: dict[str, list[str]] = {}
        for browser_name, profile_map in self._profile_checks.items():
            enabled_profiles[browser_name] = [
                pname for pname, pcb in profile_map.items() if pcb.isChecked()
            ]
        config_module.set_enabled_profiles(enabled_profiles)

        # Profile directions
        profile_directions: dict[str, dict[str, str]] = {}
        for browser_name, dir_map in self._profile_directions.items():
            profile_directions[browser_name] = {
                pname: combo.currentData() for pname, combo in dir_map.items()
            }
        config_module.set_profile_directions(profile_directions)

        # Data types
        enabled_data_types: dict[str, bool] = {
            key: cb.isChecked() for key, cb in self._data_type_checks.items()
        }
        config_module.set_enabled_data_types(enabled_data_types)

        # Autostart
        if self._autostart_check is not None:
            config_module.set_autostart(self._autostart_check.isChecked())

        _LOG.info("Settings saved")
        self.settings_saved.emit()
        self.accept()

    # ------------------------------------------------------------------
    # Load helpers
    # ------------------------------------------------------------------

    def _load_current_settings(self) -> None:
        # Sync folder
        sync_folder = config_module.get_sync_folder()
        if sync_folder and self._folder_edit:
            self._folder_edit.setText(str(sync_folder))

        # Profiles
        enabled_profiles = config_module.get_enabled_profiles()
        for browser_name, profile_map in self._profile_checks.items():
            saved_profiles = enabled_profiles.get(browser_name, list(profile_map.keys()))
            for pname, pcb in profile_map.items():
                pcb.setChecked(pname in saved_profiles)

        # Profile directions
        saved_directions = config_module.get_profile_directions()
        for browser_name, dir_map in self._profile_directions.items():
            browser_dirs = saved_directions.get(browser_name, {})
            for pname, combo in dir_map.items():
                direction = browser_dirs.get(pname, "both")
                for i in range(combo.count()):
                    if combo.itemData(i) == direction:
                        combo.setCurrentIndex(i)
                        break

        # Data types
        enabled_data_types = config_module.get_enabled_data_types()
        for key, cb in self._data_type_checks.items():
            cb.setChecked(enabled_data_types.get(key, True))

        # Autostart
        if self._autostart_check is not None:
            self._autostart_check.setChecked(config_module.get_autostart())
