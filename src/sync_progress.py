from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QFont, QTextCursor
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

_LOG = logging.getLogger(__name__)


class SyncLogSignaler(QObject):
    """Bridge between sync operations and Qt signals."""
    log_message = Signal(str, str)  # level, message
    status_changed = Signal(str)  # status text
    progress_changed = Signal(str, str, int)  # browser, profile, count


class SyncLogHandler(logging.Handler):
    """Custom logging handler that emits Qt signals for sync operations."""

    def __init__(self, signaler: SyncLogSignaler) -> None:
        super().__init__()
        self._signaler = signaler
        # Format: timestamp [LEVEL] logger: message
        formatter = logging.Formatter(
            "%(asctime)s [%(levelname)-7s] %(message)s",
            datefmt="%H:%M:%S"
        )
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._signaler.log_message.emit(record.levelname, msg)
        except Exception:  # noqa: BLE001
            self.handleError(record)


class SyncProgressDialog(QDialog):
    """Progress window showing real-time sync operations with logs and progress bar."""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Syncing — Chromium Profile Syncer")
        self.setMinimumSize(750, 450)
        # Prevent closing during sync
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, False)

        # Log signaler for thread-safe GUI updates
        self._signaler = SyncLogSignaler()
        self._signaler.log_message.connect(self._append_log)
        self._signaler.status_changed.connect(self._update_status)

        # Build UI
        layout = QVBoxLayout(self)

        # Status label
        self._status_label = QLabel("Starting sync...")
        self._status_label.setStyleSheet("font-weight: bold; font-size: 12pt;")
        layout.addWidget(self._status_label)

        # Progress bar
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 0)  # indeterminate
        self._progress_bar.setTextVisible(False)
        self._progress_bar.setMaximumHeight(12)
        layout.addWidget(self._progress_bar)

        # Profile info label
        self._profile_label = QLabel("")
        self._profile_label.setStyleSheet("color: #6272a4; font-size: 10pt;")
        layout.addWidget(self._profile_label)

        # Log area
        log_label = QLabel("Activity Log:")
        log_label.setStyleSheet("font-weight: bold; margin-top: 8px;")
        layout.addWidget(log_label)

        self._text_edit = QTextEdit()
        self._text_edit.setReadOnly(True)
        self._text_edit.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)

        # Use monospace font for better command display
        font = QFont("Monaco, Menlo, Courier New, monospace")
        font.setPointSize(10)
        self._text_edit.setFont(font)

        layout.addWidget(self._text_edit)

        # Done button (hidden initially)
        self._done_btn = QPushButton("Done")
        self._done_btn.clicked.connect(self.accept)
        self._done_btn.setVisible(False)
        layout.addWidget(self._done_btn)

        # Set up logging handler for sync operations
        self._handler = SyncLogHandler(self._signaler)
        self._handler.setLevel(logging.INFO)

        # Attach to specific loggers we care about
        logging.getLogger("src.sync_engine").addHandler(self._handler)
        logging.getLogger("src.tray").addHandler(self._handler)

        _LOG.info("Sync progress window opened")

    def closeEvent(self, event) -> None:  # noqa: N802
        """Remove logging handler when window closes."""
        logging.getLogger("src.sync_engine").removeHandler(self._handler)
        logging.getLogger("src.tray").removeHandler(self._handler)
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

    def _update_status(self, status: str) -> None:
        """Update the status label."""
        self._status_label.setText(status)

    def update_profile_progress(
        self, browser: str, profile: str, direction: str, count: int, elapsed: float
    ) -> None:
        """Update progress for current profile sync."""
        rate = count / elapsed if elapsed > 0.1 else 0
        info_text = f"Syncing {browser}/{profile} {direction}: {count} items • {elapsed:.0f}s"
        if rate > 0:
            info_text += f" • ~{rate:.1f} items/s"

        self._profile_label.setText(info_text)

    def sync_started(self) -> None:
        """Called when sync starts."""
        self._status_label.setText("Sync in progress...")
        self._progress_bar.setRange(0, 0)  # indeterminate
        self._done_btn.setVisible(False)

    def sync_finished(self, success: bool = True) -> None:
        """Called when sync completes."""
        if success:
            self._status_label.setText("✓ Sync complete")
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(100)
        else:
            self._status_label.setText("✗ Sync failed")
            self._progress_bar.setRange(0, 100)
            self._progress_bar.setValue(0)

        self._profile_label.setText("")
        self._done_btn.setVisible(True)

        # Re-enable close button
        self.setWindowFlag(Qt.WindowType.WindowCloseButtonHint, True)
        self.show()  # refresh window to apply flag change

    def on_progress(self, description: str) -> None:
        """Handle progress updates from sync engine."""
        # Update status label with truncated description
        truncated = description[:60] + "..." if len(description) > 60 else description
        self._update_status(f"Syncing: {truncated}")
