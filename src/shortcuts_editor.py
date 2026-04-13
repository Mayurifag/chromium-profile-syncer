from __future__ import annotations

import json
import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

_LOG = logging.getLogger(__name__)


class ShortcutsEditorDialog(QDialog):
    def __init__(self, parent=None, *, shortcuts_json_path: Path | None = None):
        super().__init__(parent)
        self.setWindowTitle("Search Shortcuts Editor")
        self.setMinimumSize(800, 600)
        self.shortcuts_json_path = shortcuts_json_path
        self.shortcuts: list[dict] = []

        layout = QVBoxLayout(self)

        self.table = QTableWidget()
        self.table.setColumnCount(4)
        self.table.setHorizontalHeaderLabels(["Keyword", "Name", "URL", "Default"])
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.itemChanged.connect(self._on_item_changed)
        layout.addWidget(self.table)

        button_box = QDialogButtonBox()
        self.add_btn = QPushButton("Add")
        self.delete_btn = QPushButton("Delete")
        self.save_btn = QPushButton("Save")
        self.cancel_btn = QPushButton("Cancel")

        button_box.addButton(self.add_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(self.delete_btn, QDialogButtonBox.ButtonRole.ActionRole)
        button_box.addButton(self.save_btn, QDialogButtonBox.ButtonRole.AcceptRole)
        button_box.addButton(self.cancel_btn, QDialogButtonBox.ButtonRole.RejectRole)

        self.add_btn.clicked.connect(self._add_row)
        self.delete_btn.clicked.connect(self._delete_row)
        self.save_btn.clicked.connect(self._save_shortcuts)
        self.cancel_btn.clicked.connect(self.reject)

        layout.addWidget(button_box)

        if shortcuts_json_path:
            self._load_shortcuts()

    def _load_shortcuts(self) -> None:
        if not self.shortcuts_json_path or not self.shortcuts_json_path.exists():
            _LOG.warning("Search shortcuts JSON not found: %s", self.shortcuts_json_path)
            QMessageBox.warning(
                self,
                "No Backup Found",
                f"No search shortcuts backup found.\n\n"
                f"Run a sync first to create the backup at:\n{self.shortcuts_json_path}",
            )
            return

        try:
            self.shortcuts = json.loads(self.shortcuts_json_path.read_text(encoding="utf-8"))
            self._populate_table()
            _LOG.info(
                "Loaded %d search shortcuts from %s", len(self.shortcuts), self.shortcuts_json_path
            )

        except (json.JSONDecodeError, OSError) as exc:
            _LOG.exception("Failed to load search shortcuts: %s", exc)
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to load search shortcuts:\n{exc}"
            )

    def _make_default_item(self, is_default: bool) -> QTableWidgetItem:
        item = QTableWidgetItem()
        item.setFlags(
            Qt.ItemFlag.ItemIsEnabled
            | Qt.ItemFlag.ItemIsUserCheckable
            | Qt.ItemFlag.ItemIsSelectable
        )
        item.setCheckState(Qt.CheckState.Checked if is_default else Qt.CheckState.Unchecked)
        return item

    def _populate_table(self) -> None:
        self.table.setRowCount(len(self.shortcuts))
        for i, shortcut in enumerate(self.shortcuts):
            self.table.setItem(i, 0, QTableWidgetItem(shortcut["keyword"]))
            self.table.setItem(i, 1, QTableWidgetItem(shortcut["short_name"]))
            self.table.setItem(i, 2, QTableWidgetItem(shortcut["url"]))
            self.table.setItem(i, 3, self._make_default_item(shortcut.get("is_default", False)))

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 3:
            return
        if item.checkState() != Qt.CheckState.Checked:
            return
        # Uncheck all other rows — only one default allowed.
        self.table.blockSignals(True)
        for row in range(self.table.rowCount()):
            if row != item.row():
                other = self.table.item(row, 3)
                if other:
                    other.setCheckState(Qt.CheckState.Unchecked)
        self.table.blockSignals(False)

    def _add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(""))
        self.table.setItem(row, 1, QTableWidgetItem(""))
        self.table.setItem(row, 2, QTableWidgetItem(""))
        self.table.setItem(row, 3, self._make_default_item(False))
        self.table.scrollToBottom()
        self.table.setCurrentCell(row, 0)
        self.table.editItem(self.table.item(row, 0))

    def _delete_row(self) -> None:
        current_row = self.table.currentRow()
        if current_row >= 0:
            self.table.removeRow(current_row)

    def _save_shortcuts(self) -> None:
        if not self.shortcuts_json_path:
            QMessageBox.warning(self, "Warning", "No shortcuts JSON path specified")
            return

        shortcuts = []
        for i in range(self.table.rowCount()):
            keyword_item = self.table.item(i, 0)
            name_item = self.table.item(i, 1)
            url_item = self.table.item(i, 2)

            if not keyword_item or not name_item or not url_item:
                continue

            keyword = keyword_item.text().strip()
            name = name_item.text().strip()
            url = url_item.text().strip()

            if not keyword or not url:
                continue

            old_shortcut = self.shortcuts[i] if i < len(self.shortcuts) else {}
            default_item = self.table.item(i, 3)
            is_default = (
                default_item is not None
                and default_item.checkState() == Qt.CheckState.Checked
            )
            shortcuts.append({
                "keyword": keyword,
                "short_name": name,
                "url": url,
                "favicon_url": old_shortcut.get("favicon_url", ""),
                "suggest_url": old_shortcut.get("suggest_url", ""),
                "prepopulate_id": old_shortcut.get("prepopulate_id", 0),
                "is_active": old_shortcut.get("is_active", 1),
                "date_created": old_shortcut.get("date_created", 0),
                "last_modified": old_shortcut.get("last_modified", 0),
                "sync_guid": old_shortcut.get("sync_guid", ""),
                "safe_for_autoreplace": old_shortcut.get("safe_for_autoreplace", 0),
                "input_encodings": old_shortcut.get("input_encodings", "UTF-8"),
                "alternate_urls": old_shortcut.get("alternate_urls", "[]"),
                "is_default": is_default,
            })

        try:
            self.shortcuts_json_path.write_text(
                json.dumps(shortcuts, indent=2),
                encoding="utf-8"
            )

            _LOG.info("Saved %d search shortcuts to %s", len(shortcuts), self.shortcuts_json_path)
            QMessageBox.information(
                self,
                "Success",
                f"Saved {len(shortcuts)} search shortcuts to backup.\n\n"
                f"They will be synced to browsers on next sync."
            )
            self.accept()

        except OSError as exc:
            _LOG.exception("Failed to save search shortcuts: %s", exc)
            QMessageBox.critical(
                self,
                "Error",
                f"Failed to save search shortcuts:\n{exc}"
            )
