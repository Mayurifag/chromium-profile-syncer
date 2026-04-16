from __future__ import annotations

import logging

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import QPushButton, QTextEdit, QVBoxLayout, QWidget

from src.dracula import DEFAULT_LOG_COLOR, LOG_COLORS
from src.log_viewer import GUILogHandler, LogSignaler


class ActivityLogWidget(QWidget):
    resized = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._log_signaler: LogSignaler | None = None
        self._log_handler: GUILogHandler | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self._text = QTextEdit()
        self._text.setReadOnly(True)
        self._text.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self._text.setMinimumHeight(200)
        self._text.setMaximumHeight(300)
        font = QFont("Monaco, Menlo, Courier New, monospace")
        font.setPointSize(11)
        self._text.setFont(font)
        layout.addWidget(self._text)

        self._clear_btn = QPushButton("Clear Log")
        self._clear_btn.clicked.connect(self._clear)
        self._clear_btn.setVisible(False)
        layout.addWidget(self._clear_btn)

        self.setVisible(False)

    def enable(self) -> None:
        if self._log_signaler is None:
            self._log_signaler = LogSignaler()
            self._log_signaler.log_message.connect(self._append)
        if self._log_handler is None:
            self._log_handler = GUILogHandler(self._log_signaler)
            self._log_handler.setLevel(logging.DEBUG)
            logging.getLogger().addHandler(self._log_handler)
        if self._text.toPlainText().strip():
            self.setVisible(True)
            self.resized.emit()

    def disable(self) -> None:
        self._remove_handler()
        self.setVisible(False)
        self.resized.emit()

    def cleanup(self) -> None:
        self._remove_handler()

    def is_enabled(self) -> bool:
        return self._log_handler is not None

    def _remove_handler(self) -> None:
        if self._log_handler is not None:
            logging.getLogger().removeHandler(self._log_handler)
            self._log_handler = None

    def _append(self, level: str, message: str) -> None:
        color = LOG_COLORS.get(level, DEFAULT_LOG_COLOR)
        cursor = self._text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = cursor.charFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)
        cursor.insertText(message + "\n")
        self._text.setTextCursor(cursor)
        self._text.ensureCursorVisible()
        if not self.isVisible():
            self.setVisible(True)
            self.resized.emit()
        if not self._clear_btn.isVisible():
            self._clear_btn.setVisible(True)

    def _clear(self) -> None:
        self._text.clear()
        self._clear_btn.setVisible(False)
        self.setVisible(False)
        self.resized.emit()
