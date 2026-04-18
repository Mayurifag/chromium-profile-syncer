from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from src.dracula import DEFAULT_LOG_COLOR, LOG_COLORS, LOG_VIEWER_TEXTEDIT

_LOG = logging.getLogger(__name__)


class LogSignaler(QObject):
    log_message = Signal(str, str)  # level, message


class GUILogHandler(logging.Handler):
    def __init__(self, signaler: LogSignaler) -> None:
        super().__init__()
        self._signaler = signaler
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._signaler.log_message.emit(record.levelname, msg)
        except Exception:  # noqa: BLE001
            self.handleError(record)


class LogViewerDialog(QDialog):
    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Activity Log — Chromium Profile Syncer")
        self.setMinimumSize(800, 500)

        self._signaler = LogSignaler()
        self._signaler.log_message.connect(self._append_log)

        layout = QVBoxLayout(self)

        self._text_edit = QTextEdit()
        self._text_edit.document().setMaximumBlockCount(1000)
        self._text_edit.setReadOnly(True)
        self._text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        font = QFont("Monaco, Menlo, Courier New, monospace")
        font.setPointSize(11)
        self._text_edit.setFont(font)
        self._text_edit.setStyleSheet(LOG_VIEWER_TEXTEDIT)

        layout.addWidget(self._text_edit)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_log)
        layout.addWidget(clear_btn)

        self._handler = GUILogHandler(self._signaler)
        self._handler.setLevel(logging.DEBUG)
        logging.getLogger().addHandler(self._handler)

        _LOG.info("Activity log viewer opened")

    def closeEvent(self, event) -> None:  # noqa: N802
        logging.getLogger().removeHandler(self._handler)
        _LOG.info("Activity log viewer closed")
        super().closeEvent(event)

    def _append_log(self, level: str, message: str) -> None:
        color = LOG_COLORS.get(level, DEFAULT_LOG_COLOR)
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = cursor.charFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText(message + "\n")
        self._text_edit.setTextCursor(cursor)
        self._text_edit.ensureCursorVisible()

    def _clear_log(self) -> None:
        self._text_edit.clear()
        _LOG.info("Log cleared")
