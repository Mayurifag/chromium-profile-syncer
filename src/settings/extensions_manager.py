from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


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


def _generate_html(ext_map: dict[str, str]) -> str:
    rows = ""
    for ext_id, name in sorted(ext_map.items(), key=lambda x: (x[1] or x[0]).lower()):
        display = name or ext_id
        url = f"https://chromewebstore.google.com/detail/{ext_id}"
        rows += (
            f'<tr><td><a href="{url}" target="_blank">{display}</a></td>'
            f'<td style="color:#888;font-size:0.85em">{ext_id}</td></tr>\n'
        )
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Extensions</title>
<style>
  body {{ font-family: sans-serif; padding: 24px; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th {{ text-align: left; border-bottom: 2px solid #ccc; padding: 6px 8px; }}
  td {{ padding: 6px 8px; border-bottom: 1px solid #eee; }}
  a {{ color: #1a73e8; text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h2>Extensions ({len(ext_map)})</h2>
<table>
<tr><th>Name</th><th>ID</th></tr>
{rows}</table>
</body>
</html>"""


class ExtensionsManagerDialog(QDialog):
    def __init__(self, parent, sync_folder: Path):
        super().__init__(parent)
        self.setWindowTitle("Extensions")
        self.setMinimumWidth(560)
        self._sync_folder = sync_folder
        self._work_dir: Path | None = None
        self._deleted: set[str] = set()

        self._setup_work_dir()
        self._build_ui()

    def _setup_work_dir(self) -> None:
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

    def _build_ui(self) -> None:
        ext_map = self._read_extensions()

        root = QVBoxLayout(self)

        if not ext_map:
            root.addWidget(QLabel("No extensions found in backup."))
            buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
            buttons.rejected.connect(self.reject)
            root.addWidget(buttons)
            return

        toolbar = QHBoxLayout()
        html_btn = QPushButton("Open in Browser")
        html_btn.clicked.connect(lambda: self._open_html(ext_map))
        toolbar.addWidget(html_btn)
        toolbar.addStretch()
        root.addLayout(toolbar)

        table = QTableWidget(len(ext_map), 3)
        table.setHorizontalHeaderLabels(["Name", "Settings", ""])
        hdr = table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
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

            del_btn = QPushButton("Delete")
            del_btn.clicked.connect(
                lambda _checked, eid=ext_id, r=row: self._delete_extension(eid, r)
            )
            table.setCellWidget(row, 2, del_btn)

        root.addWidget(table)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _open_html(self, ext_map: dict[str, str]) -> None:
        assert self._work_dir
        html_path = self._work_dir / "extensions.html"
        html_path.write_text(_generate_html(ext_map), encoding="utf-8")
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(html_path)))

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

        self.accept()

    def done(self, result: int) -> None:
        if self._work_dir:
            shutil.rmtree(self._work_dir, ignore_errors=True)
            self._work_dir = None
        super().done(result)
