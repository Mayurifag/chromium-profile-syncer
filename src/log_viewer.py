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

_LOG = logging.getLogger(__name__)


class LogSignaler(QObject):
    """Bridge between Python logging and Qt signals."""
    log_message = Signal(str, str)  # level, message


class GUILogHandler(logging.Handler):
    """Custom logging handler that emits Qt signals for GUI display."""

    def __init__(self, signaler: LogSignaler) -> None:
        super().__init__()
        self._signaler = signaler
        # Format: timestamp [LEVEL] logger: message
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
            datefmt="%H:%M:%S"
        )
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._signaler.log_message.emit(record.levelname, msg)
        except Exception:  # noqa: BLE001
            self.handleError(record)


class LogViewerDialog(QDialog):
    """Real-time activity log viewer showing all operations and commands."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Activity Log — Chromium Profile Syncer")
        self.setMinimumSize(800, 500)

        # Log signaler for thread-safe GUI updates
        self._signaler = LogSignaler()
        self._signaler.log_message.connect(self._append_log)

        # Build UI
        layout = QVBoxLayout(self)

        # Text display
        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        # Use monospace font for better command display
        font = QFont("Monaco, Menlo, Courier New, monospace")
        font.setPointSize(11)
        self._text_edit.setFont(font)

        # Dark theme for better readability
        self._text_edit.setStyleSheet("""
            QTextEdit {
                background-color: #1e1e1e;
                color: #d4d4d4;
                border: none;
            }
        """)

        layout.addWidget(self._text_edit)

        # Control buttons
        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_log)
        layout.addWidget(clear_btn)

        # Set up logging handler
        self._handler = GUILogHandler(self._signaler)
        self._handler.setLevel(logging.DEBUG)

        # Attach to root logger to capture all logging
        logging.getLogger().addHandler(self._handler)

        _LOG.info("Activity log viewer opened")

    def closeEvent(self, event) -> None:  # noqa: N802
        """Remove logging handler when window closes."""
        logging.getLogger().removeHandler(self._handler)
        _LOG.info("Activity log viewer closed")
        super().closeEvent(event)

    def _append_log(self, level: str, message: str) -> None:
        """Append a log message with color coding based on level."""
        # Color map for log levels
        colors = {
            "DEBUG": "#808080",     # Gray
            "INFO": "#4ec9b0",      # Teal
            "WARNING": "#dcdcaa",   # Yellow
            "ERROR": "#f48771",     # Red
            "CRITICAL": "#ff0000",  # Bright red
        }
        color = colors.get(level, "#d4d4d4")

        # Insert colored text
        cursor = self._text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)

        # Apply color
        fmt = cursor.charFormat()
        fmt.setForeground(QColor(color))
        cursor.setCharFormat(fmt)

        # Insert message
        cursor.insertText(message + "\n")

        # Auto-scroll to bottom
        self._text_edit.setTextCursor(cursor)
        self._text_edit.ensureCursorVisible()

    def _clear_log(self) -> None:
        """Clear all log messages."""
        self._text_edit.clear()
        _LOG.info("Log cleared")
