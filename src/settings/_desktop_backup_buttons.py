from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QMessageBox, QPushButton, QWidget

import src.config as config_module
from src.settings import _desktop_backup
from src.sync.sync_dir import SYNC_DIR_NAME

_LOG = logging.getLogger(__name__)


class DesktopBackupButtons:
    def __init__(
        self, parent: QWidget, on_restored: Callable[[Path], None]
    ) -> None:
        self._parent = parent
        self._on_restored = on_restored

        self.backup_btn = QPushButton("Backup to Desktop")
        self.backup_btn.clicked.connect(self._do_backup)

        self.restore_btn = QPushButton("Restore Desktop Backup")
        self.restore_btn.setVisible(False)
        self.restore_btn.clicked.connect(self._do_restore)

        self.delete_btn = QPushButton("Delete Desktop Backup")
        self.delete_btn.setVisible(False)
        self.delete_btn.clicked.connect(self._do_delete)

    def widgets(self) -> list[QPushButton]:
        return [self.backup_btn, self.restore_btn, self.delete_btn]

    def refresh(self) -> None:
        sync_folder = config_module.get_sync_folder()
        has_current = sync_folder is not None and (sync_folder / SYNC_DIR_NAME).is_dir()
        self.backup_btn.setEnabled(has_current)
        exists = _desktop_backup.desktop_backup_path().is_file()
        self.restore_btn.setVisible(exists)
        self.delete_btn.setVisible(exists)

    def _do_backup(self) -> None:
        sync_folder = config_module.get_sync_folder()
        if sync_folder is None:
            QMessageBox.warning(self._parent, "No Sync Folder", "Configure a sync folder first.")
            return
        current_dir = sync_folder / SYNC_DIR_NAME
        if not current_dir.is_dir():
            QMessageBox.information(
                self._parent, "No Backup", "Run a sync first to create the backup."
            )
            return

        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        self.backup_btn.setEnabled(False)
        QCoreApplication.processEvents()
        try:
            target = _desktop_backup.create(current_dir)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            self.backup_btn.setEnabled(True)
            _LOG.exception("Desktop backup failed")
            QMessageBox.critical(
                self._parent, "Backup Failed", f"Could not create backup:\n{exc}"
            )
            return
        QApplication.restoreOverrideCursor()
        _LOG.info("Desktop backup written: %s", target)
        self.refresh()
        QMessageBox.information(self._parent, "Backup Created", f"Saved to:\n{target}")

    def _do_restore(self) -> None:
        sync_folder = config_module.get_sync_folder()
        if sync_folder is None:
            QMessageBox.warning(self._parent, "No Sync Folder", "Configure a sync folder first.")
            return

        backup_path = _desktop_backup.desktop_backup_path()
        if not backup_path.is_file():
            self.refresh()
            return

        reply = QMessageBox.question(
            self._parent,
            "Restore Desktop Backup",
            f"Overwrite sync folder contents with desktop backup?\n\n"
            f"From: {backup_path}\nTo:   {sync_folder / SYNC_DIR_NAME}\n\n"
            "Existing synced data will be replaced.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        QApplication.setOverrideCursor(QCursor(Qt.CursorShape.WaitCursor))
        QCoreApplication.processEvents()
        try:
            _desktop_backup.restore(sync_folder / SYNC_DIR_NAME)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            _LOG.exception("Desktop backup restore failed")
            QMessageBox.critical(
                self._parent, "Restore Failed", f"Could not restore backup:\n{exc}"
            )
            return
        QApplication.restoreOverrideCursor()
        _LOG.info("Desktop backup restored to %s", sync_folder / SYNC_DIR_NAME)
        self._on_restored(sync_folder)
        QMessageBox.information(self._parent, "Restore Complete", "Desktop backup restored.")

    def _do_delete(self) -> None:
        backup_path = _desktop_backup.desktop_backup_path()
        if not backup_path.is_file():
            self.refresh()
            return

        reply = QMessageBox.question(
            self._parent,
            "Delete Desktop Backup",
            f"Delete {backup_path}?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        try:
            _desktop_backup.delete()
        except Exception as exc:
            _LOG.exception("Desktop backup delete failed")
            QMessageBox.critical(
                self._parent, "Delete Failed", f"Could not delete backup:\n{exc}"
            )
            return
        _LOG.info("Desktop backup deleted")
        self.refresh()
