from __future__ import annotations

import logging

import pytest
from PySide6.QtWidgets import QApplication

from src.log_viewer import GUILogHandler, LogSignaler, LogViewerDialog


@pytest.fixture(scope="module")
def qapp():
    """Ensure QApplication exists for Qt widgets."""
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def test_log_signaler_creation(qapp):
    """Test that LogSignaler can be created."""
    signaler = LogSignaler()
    handler = GUILogHandler(signaler)

    messages = []

    def capture(level: str, msg: str) -> None:
        messages.append((level, msg))

    signaler.log_message.connect(capture)

    # Create test logger
    logger = logging.getLogger("test_logger")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)

    # Emit a message
    logger.info("Test message")

    # Check message was captured
    assert len(messages) == 1
    assert messages[0][0] == "INFO"
    assert "Test message" in messages[0][1]

    logger.removeHandler(handler)


def test_log_viewer_dialog_creation(qapp):
    """Test that LogViewerDialog can be created."""
    # Get root logger handlers before creating dialog
    root_logger = logging.getLogger()
    initial_handlers = len(root_logger.handlers)

    dialog = LogViewerDialog()

    # Verify window properties
    assert dialog.windowTitle() == "Activity Log — Chromium Profile Syncer"
    assert dialog.minimumWidth() == 800
    assert dialog.minimumHeight() == 500

    # Verify text edit is read-only
    assert dialog._text_edit.isReadOnly()

    # Handler should be added
    assert dialog._handler in root_logger.handlers
    assert len(root_logger.handlers) == initial_handlers + 1

    dialog.close()

    # Handler should be removed after close
    assert dialog._handler not in root_logger.handlers
    assert len(root_logger.handlers) == initial_handlers
