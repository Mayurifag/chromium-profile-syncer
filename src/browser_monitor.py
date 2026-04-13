from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QTimer, Signal

if TYPE_CHECKING:
    from src.browsers.base import BrowserBase

logger = logging.getLogger(__name__)


class BrowserMonitor(QObject):
    """Polls installed browser processes every 2 s and emits signals on state transitions."""

    browser_closed = Signal(str)    # browser_name: running → stopped
    browser_opened = Signal(str)    # browser_name: stopped → running
    state_changed = Signal(str, bool)  # browser_name, is_running

    def __init__(self, browsers: list[BrowserBase], parent=None) -> None:
        super().__init__(parent)
        self._browsers = browsers
        self._state: dict[str, bool] = {b.name: b.is_running() for b in browsers}
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        logger.debug("BrowserMonitor started for: %s", [b.name for b in browsers])

    def is_running(self, browser_name: str) -> bool:
        return self._state.get(browser_name, False)

    def any_running(self) -> bool:
        return any(self._state.values())

    def running_names(self) -> list[str]:
        return [name for name, running in self._state.items() if running]

    def _poll(self) -> None:
        for browser in self._browsers:
            current = browser.is_running()
            prev = self._state.get(browser.name, current)
            if prev != current:
                self._state[browser.name] = current
                self.state_changed.emit(browser.name, current)
                if not current:
                    logger.debug("Browser closed: %s", browser.name)
                    self.browser_closed.emit(browser.name)
                else:
                    logger.debug("Browser opened: %s", browser.name)
                    self.browser_opened.emit(browser.name)
