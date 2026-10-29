from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Qt, QThread
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget

from src import config
from src.settings import _desktop_backup
from src.sync.sync_dir import SYNC_DIR_NAME


class _BackupWorker(QThread):
    def __init__(self, action: Callable, parent: QWidget):
        super().__init__(parent)
        self.action = action
        self.failure: Exception | None = None
        self.result = None

    def run(self) -> None:
        try:
            self.result = self.action()
        except Exception as exc:
            self.failure = exc


class DesktopBackupButtons:
    def __init__(self, parent: QWidget, on_restored: Callable[[Path], None]):
        self._parent = parent
        self._on_restored = on_restored
        self._worker: _BackupWorker | None = None
        self.backup_btn = QPushButton("Backup → Desktop")
        self.backup_btn.clicked.connect(self._do_backup)
        self.restore_btn = QPushButton("Restore from Desktop")
        self.restore_btn.clicked.connect(self._do_restore)
        self.delete_btn = QPushButton("Delete Desktop Copy")
        self.delete_btn.clicked.connect(self._do_delete)
        self.restore_btn.hide()
        self.delete_btn.hide()

    def widgets(self) -> list[QPushButton]:
        return [self.backup_btn, self.restore_btn, self.delete_btn]

    def refresh(self) -> None:
        folder = config.get_sync_folder()
        self.backup_btn.setEnabled(folder is not None and (folder / SYNC_DIR_NAME).is_dir())
        exists = _desktop_backup.desktop_backup_path().is_file()
        self.restore_btn.setVisible(exists)
        self.delete_btn.setVisible(exists)

    def _start(self, action: Callable, title: str, on_success: Callable) -> None:
        if self._worker is not None:
            return
        worker = _BackupWorker(action, self._parent)
        self._worker = worker
        self._parent.setEnabled(False)
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

        def finished():
            self._worker = None
            self._parent.setEnabled(True)
            QApplication.restoreOverrideCursor()
            if worker.failure is not None:
                QMessageBox.critical(self._parent, title, str(worker.failure))
            else:
                on_success(worker.result)
            self.refresh()
            worker.deleteLater()

        worker.finished.connect(finished)
        worker.start()

    def _do_backup(self) -> None:
        folder = config.get_sync_folder()
        if folder is None or not (folder / SYNC_DIR_NAME).is_dir():
            return
        self._start(
            lambda: _desktop_backup.create(folder / SYNC_DIR_NAME),
            "Backup failed",
            lambda path: QMessageBox.information(self._parent, "Backup created", str(path)),
        )

    def _do_restore(self) -> None:
        folder = config.get_sync_folder()
        if folder is None:
            return
        answer = QMessageBox.question(
            self._parent,
            "Restore desktop backup",
            f"Replace {folder / SYNC_DIR_NAME} with the desktop backup?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start(
            lambda: _desktop_backup.restore(folder / SYNC_DIR_NAME),
            "Restore failed",
            lambda result: self._on_restored(folder),
        )

    def _do_delete(self) -> None:
        answer = QMessageBox.question(
            self._parent,
            "Delete desktop backup",
            f"Delete {_desktop_backup.desktop_backup_path()}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer == QMessageBox.StandardButton.Yes:
            self._start(_desktop_backup.delete, "Delete failed", lambda result: None)
