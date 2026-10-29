from __future__ import annotations

import logging

import pytest
from PySide6.QtWidgets import QApplication

from src.log_viewer import GUILogHandler, LogSignaler


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


def test_embedded_activity_log_is_bounded_and_releases_handler(qapp):
    from src.settings._activity_log import ActivityLogWidget

    root_logger = logging.getLogger()
    initial_handlers = len(root_logger.handlers)

    widget = ActivityLogWidget()
    widget.enable()
    assert widget._text.isReadOnly()
    assert widget._text.document().maximumBlockCount() == 1000
    handler = widget._log_handler
    assert handler in root_logger.handlers
    assert len(root_logger.handlers) == initial_handlers + 1

    widget.cleanup()
    assert handler not in root_logger.handlers
    assert len(root_logger.handlers) == initial_handlers
