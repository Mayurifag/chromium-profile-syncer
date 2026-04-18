from __future__ import annotations

import json
import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

import src.config as config_module


def _dir_size(d: Path) -> int:
    total = 0
    for f in d.rglob("*"):
        if f.is_file():
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def _fmt_size(size: int) -> str:
    if size == 0:
        return ""
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    return f"{size / 1024 ** 2:.1f} MB"


def _settings_size(work_dir: Path, ext_id: str) -> int:
    dirs: list[Path] = [
        work_dir / "Local Extension Settings" / ext_id,
        work_dir / "Sync Extension Settings" / ext_id,
    ]
    idb = work_dir / "IndexedDB"
    if idb.exists():
        dirs += [d for d in idb.iterdir() if d.name.startswith(f"chrome-extension_{ext_id}_")]
    return sum(_dir_size(d) for d in dirs if d.exists())


class _BrowserPickerDialog(QDialog):
    def __init__(self, parent, browsers: list[str], selected: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Install for browsers")
        self._checkboxes: list[QCheckBox] = []

        root = QVBoxLayout(self)
        root.addWidget(QLabel("Install only for (leave all unchecked = all browsers):"))
        for name in browsers:
            cb = QCheckBox(name)
            cb.setChecked(name in selected)
            self._checkboxes.append(cb)
            root.addWidget(cb)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def selected(self) -> list[str]:
        return [cb.text() for cb in self._checkboxes if cb.isChecked()]


class ExtensionsManagerDialog(QDialog):
    def __init__(self, parent, sync_folder: Path, available_browsers: list[str]):
        super().__init__(parent)
        self.setWindowTitle("Extensions Manager")
        self.setMinimumWidth(620)
        self._sync_folder = sync_folder
        self._available_browsers = available_browsers
        self._work_dir: Path | None = None
        self._restrictions: dict[str, list[str]] = dict(
            config_module.get_extension_browser_restrictions()
        )
        self._deleted: set[str] = set()

        self._setup_work_dir()
        self._build_ui()

    def _setup_work_dir(self) -> None:
        import tempfile

        from src.sync.archive import ARCHIVE_NAME, unpack_archive

        self._work_dir = Path(tempfile.mkdtemp(prefix="cps-ext-"))
        unpack_archive(self._sync_folder / ARCHIVE_NAME, self._work_dir)

    def _read_extensions(self) -> dict[str, str]:
        assert self._work_dir
        manifest = self._work_dir / "webstore_extensions.json"
        if not manifest.exists():
            return {}
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {e: "" for e in data}
        except (json.JSONDecodeError, OSError):
            return {}

    def _restriction_label(self, ext_id: str) -> str:
        sel = self._restrictions.get(ext_id, [])
        return ", ".join(sel) if sel else "All browsers"

    def _build_ui(self) -> None:
        ext_map = self._read_extensions()

        root = QVBoxLayout(self)

        if not ext_map:
            root.addWidget(QLabel("No extensions found in backup."))
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            root.addWidget(buttons)
            return

        table = QTableWidget(len(ext_map), 5)
        table.setHorizontalHeaderLabels(["Name", "Settings", "Install for", "", ""])
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.verticalHeader().setVisible(False)
        self._table = table

        assert self._work_dir
        for row, (ext_id, name) in enumerate(
            sorted(ext_map.items(), key=lambda x: (x[1] or x[0]).lower())
        ):
            name_item = QTableWidgetItem(name or ext_id)
            name_item.setToolTip(ext_id)
            table.setItem(row, 0, name_item)

            size = _settings_size(self._work_dir, ext_id)
            size_item = QTableWidgetItem(_fmt_size(size) or "—")
            size_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            table.setItem(row, 1, size_item)

            restrict_btn = QPushButton(self._restriction_label(ext_id))
            restrict_btn.clicked.connect(
                lambda _checked, eid=ext_id, btn=restrict_btn: self._edit_restriction(eid, btn)
            )
            table.setCellWidget(row, 2, restrict_btn)

            del_btn = QPushButton("Delete")
            del_btn.clicked.connect(
                lambda _checked, eid=ext_id, r=row: self._delete_extension(eid, r)
            )
            table.setCellWidget(row, 3, del_btn)

            open_btn = QPushButton("Store")
            open_btn.clicked.connect(
                lambda _checked, eid=ext_id: QDesktopServices.openUrl(
                    QUrl(f"https://chromewebstore.google.com/detail/{eid}")
                )
            )
            table.setCellWidget(row, 4, open_btn)

        root.addWidget(table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _edit_restriction(self, ext_id: str, btn: QPushButton) -> None:
        dlg = _BrowserPickerDialog(
            self, self._available_browsers, self._restrictions.get(ext_id, [])
        )
        if dlg.exec() == QDialog.DialogCode.Accepted:
            sel = dlg.selected()
            if sel:
                self._restrictions[ext_id] = sel
            else:
                self._restrictions.pop(ext_id, None)
            btn.setText(self._restriction_label(ext_id))

    def _delete_extension(self, ext_id: str, row: int) -> None:
        from PySide6.QtWidgets import QMessageBox

        reply = QMessageBox.question(
            self,
            "Delete Extension",
            f"Remove '{ext_id}' and all its settings from backup?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._deleted.add(ext_id)
        self._table.hideRow(row)

    def _apply_deletions(self) -> None:
        assert self._work_dir
        manifest_path = self._work_dir / "webstore_extensions.json"
        if manifest_path.exists():
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    for eid in self._deleted:
                        data.pop(eid, None)
                else:
                    data = [e for e in data if e not in self._deleted]
                manifest_path.write_text(json.dumps(data), encoding="utf-8")
            except (json.JSONDecodeError, OSError):
                pass

        for ext_id in self._deleted:
            for subdir in ("Extensions", "Local Extension Settings", "Sync Extension Settings"):
                d = self._work_dir / subdir / ext_id
                if d.exists():
                    shutil.rmtree(d, ignore_errors=True)
            self._restrictions.pop(ext_id, None)
        idb = self._work_dir / "IndexedDB"
        if idb.exists():
            prefixes = tuple(f"chrome-extension_{e}_" for e in self._deleted)
            for d in list(idb.iterdir()):
                if d.name.startswith(prefixes):
                    shutil.rmtree(d, ignore_errors=True)

    def _save(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self._deleted:
            QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            try:
                self._apply_deletions()
                from src.sync.archive import ARCHIVE_NAME, pack_to_archive
                pack_to_archive(self._work_dir, self._sync_folder / ARCHIVE_NAME)
            finally:
                QApplication.restoreOverrideCursor()

        config_module.set_extension_browser_restrictions(self._restrictions)
        self.accept()

    def done(self, result: int) -> None:
        if self._work_dir:
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._work_dir = None
        super().done(result)
