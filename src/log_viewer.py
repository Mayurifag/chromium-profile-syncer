from __future__ import annotations

import logging

from PySide6.QtCore import QObject, Signal


class LogSignaler(QObject):
    log_message = Signal(str, str)  # level, message


class GUILogHandler(logging.Handler):
    def __init__(self, signaler: LogSignaler) -> None:
        super().__init__()
        self._signaler = signaler
        self.setFormatter(
            logging.Formatter(
                "%(asctime)s [%(levelname)-7s] %(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._signaler.log_message.emit(record.levelname, msg)
        except Exception:  # noqa: BLE001
            self.handleError(record)
