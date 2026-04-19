from __future__ import annotations

import platform
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QLabel

from src.dracula import NOT_RUNNING_DOT, RUNNING_DOT, RUNNING_GLOW
from src.sync.sync_dir import SYNC_DIR_NAME as _SYNC_DIR_NAME

_CLOSE_BROWSER_HINT = (
    "Close browser from the system tray to allow sync"
    if platform.system() == "Windows"
    else "Quit browser (Cmd+Q) to allow sync"
)


def _make_indicator_pixmap(is_running: bool) -> QPixmap:
    pixmap = QPixmap(12, 12)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    if is_running:
        painter.setBrush(QColor(*RUNNING_GLOW))
        painter.drawEllipse(0, 0, 12, 12)
        painter.setBrush(QColor(*RUNNING_DOT))
        painter.drawEllipse(2, 2, 8, 8)
    else:
        painter.setBrush(QColor(*NOT_RUNNING_DOT))
        painter.drawEllipse(2, 2, 8, 8)
    painter.end()
    return pixmap


def _make_status_indicator(is_running: bool) -> QLabel:
    label = QLabel()
    label.setPixmap(_make_indicator_pixmap(is_running))
    label.setToolTip(_CLOSE_BROWSER_HINT if is_running else "Browser is not running")
    return label


def _sync_folder_has_data(folder: Path) -> bool:
    return (folder / _SYNC_DIR_NAME).is_dir()


_sync_folder_has_profile = _sync_folder_has_data
