from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src import config
from src.browsers import ALL_BROWSERS, get_browser
from src.operations import operation_lock
from src.sync import extensions
from src.sync.sync_dir import SYNC_DIR_NAME

_LOG = logging.getLogger(__name__)


def extension_backup_count(sync_folder: Path) -> int:
    directory = sync_folder / SYNC_DIR_NAME
    if not directory.is_dir():
        directory = sync_folder
    return len(extensions.read_library(directory))


def _all_ext_aliases() -> dict[str, str]:
    return {
        alias: canonical
        for browser in ALL_BROWSERS
        for alias, canonical in browser.ext_id_aliases.items()
    }


def _open_urls(urls: list[str]) -> None:
    browser = get_browser(config.get_last_restored_browser())
    command = browser.launch_command() if browser else None
    if command:
        try:
            subprocess.Popen([*command, *urls], close_fds=True)
            return
        except OSError:
            _LOG.exception("Could not open extension store in %s", browser.name)
    for url in urls:
        QDesktopServices.openUrl(QUrl(url))


def _settings_size(directory: Path, ext_id: str) -> int:
    candidates = {ext_id} | {
        alias for alias, canonical in _all_ext_aliases().items() if canonical == ext_id
    }
    paths = [
        directory / parent / candidate
        for candidate in candidates
        for parent in (
            "Local Extension Settings",
            "Sync Extension Settings",
            "Managed Extension Settings",
        )
    ]
    indexeddb = directory / "IndexedDB"
    if indexeddb.is_dir():
        paths.extend(
            path
            for path in indexeddb.iterdir()
            if extensions.indexeddb_extension_id(path.name) in candidates
        )
    total = 0
    for path in paths:
        for item in path.rglob("*"):
            if item.is_file():
                total += item.stat().st_size
    return total


def _fmt_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024**2:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024**2:.1f} MB"


class ExtensionsManagerDialog(QDialog):
    def __init__(self, parent, sync_folder: Path):
        super().__init__(parent)
        self.setWindowTitle("Extensions — active and saved")
        self.resize(820, 500)
        self._work_dir = sync_folder / SYNC_DIR_NAME
        self._changes: dict[str, bool] = {}
        self.restoring = False
        with operation_lock():
            self._entries = extensions.read_library(self._work_dir)
        self._build_ui()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        note = QLabel(
            "Remove or disable extensions in your browser as usual. Changes are detected after "
            "it closes. Removed extensions keep their last backed-up settings here.\n"
            "Archive stops installation across synced browsers; manually installed extensions "
            "may need removal in each browser. Restore opens the Web Store after Save."
        )
        note.setWordWrap(True)
        root.addWidget(note)
        self._search = QLineEdit()
        self._search.setPlaceholderText("Search name, extension ID or status")
        self._search.setAccessibleName("Search extensions")
        self._search.textChanged.connect(self._filter_rows)
        root.addWidget(self._search)

        self._table = QTableWidget(len(self._entries), 4)
        self._table.setHorizontalHeaderLabels(["Name", "Status", "Saved settings", "Actions"])
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for column in (1, 2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for row, (ext_id, entry) in enumerate(
            sorted(
                self._entries.items(),
                key=lambda pair: (pair[1].get("name", "").casefold(), pair[0]),
            )
        ):
            item = QTableWidgetItem(entry.get("name") or ext_id)
            item.setToolTip(ext_id)
            item.setData(Qt.ItemDataRole.UserRole, ext_id)
            self._table.setItem(row, 0, item)
            self._table.setItem(row, 1, QTableWidgetItem())
            self._table.setItem(
                row, 2, QTableWidgetItem(_fmt_size(_settings_size(self._work_dir, ext_id)))
            )
            actions = QWidget()
            layout = QHBoxLayout(actions)
            layout.setContentsMargins(0, 0, 0, 0)
            toggle = QPushButton()
            toggle.clicked.connect(lambda _checked=False, r=row: self._toggle(r))
            layout.addWidget(toggle)
            store = QPushButton("Web Store")
            store.clicked.connect(
                lambda _checked=False, eid=ext_id: _open_urls(
                    [f"https://chromewebstore.google.com/detail/{eid}"]
                )
            )
            layout.addWidget(store)
            self._table.setCellWidget(row, 3, actions)
            self._update_row(row)
        root.addWidget(self._table)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _is_archived(self, ext_id: str) -> bool:
        return self._changes.get(ext_id, self._entries[ext_id]["archived"])

    def _update_row(self, row: int) -> None:
        ext_id = self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        archived = self._is_archived(ext_id)
        status = "Saved" if archived else "Active"
        if ext_id in self._changes:
            status += " (pending)"
        self._table.item(row, 1).setText(status)
        button = self._table.cellWidget(row, 3).layout().itemAt(0).widget()
        button.setText("Restore" if archived else "Archive")
        button.setAccessibleName(f"{button.text()} {self._table.item(row, 0).text()}")

    def _toggle(self, row: int) -> None:
        ext_id = self._table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        value = not self._is_archived(ext_id)
        if value == self._entries[ext_id]["archived"]:
            self._changes.pop(ext_id, None)
        else:
            self._changes[ext_id] = value
        self._update_row(row)
        self._filter_rows(self._search.text())

    def _filter_rows(self, text: str) -> None:
        query = text.casefold().strip()
        for row in range(self._table.rowCount()):
            item = self._table.item(row, 0)
            haystack = f"{item.text()} {item.toolTip()} {self._table.item(row, 1).text()}"
            self._table.setRowHidden(row, query not in haystack.casefold())

    def _save(self) -> None:
        try:
            with operation_lock():
                if self._changes:
                    extensions.update_archive_states(self._work_dir, self._changes)
                restore = {eid for eid, archived in self._changes.items() if not archived}
                archived_ids = {eid for eid, archived in self._changes.items() if archived}
                for browser, profiles in config.get_enabled_profiles().items():
                    for profile in profiles:
                        config.request_extension_restore(browser, profile, restore)
                        config.complete_extension_restores(browser, profile, archived_ids)
        except (OSError, ValueError) as exc:
            QMessageBox.critical(self, "Could not save extensions", str(exc))
            return
        restore = [ext_id for ext_id, archived in self._changes.items() if not archived]
        self.restoring = bool(restore)
        self.accept()
        if restore:
            _open_urls([f"https://chromewebstore.google.com/detail/{ext_id}" for ext_id in restore])
