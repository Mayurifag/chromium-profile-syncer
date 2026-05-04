from __future__ import annotations

import logging
from collections.abc import Callable

from PySide6.QtCore import QThread, Signal

from src import updater

logger = logging.getLogger(__name__)


def is_update_supported() -> bool:
    return updater._is_frozen()


class UpdateCheckThread(QThread):
    completed = Signal(bool)
    user_message = Signal(str)

    def __init__(
        self,
        *,
        silent: bool,
        is_sync_running: Callable[[], bool],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._silent = silent
        self._is_sync_running = is_sync_running

    def run(self) -> None:
        try:
            result = updater.check_for_update()
        except updater.UpdateCheckError as exc:
            logger.warning("update: %s", exc)
            if not self._silent:
                self.user_message.emit(f"Update check failed: {exc}")
            self.completed.emit(False)
            return
        if result is None:
            if not self._silent:
                self.user_message.emit("Already up to date.")
            self.completed.emit(False)
            return
        target_sha, asset_url, sha_url = result
        if self._is_sync_running():
            logger.info("update: deferring (sync running) — target=%s", target_sha[:8])
            self.completed.emit(False)
            return
        logger.info("update: installing %s", target_sha[:8])
        try:
            updater.install_update(asset_url, sha_url)
        except Exception as exc:
            logger.error("update: install failed: %s", exc)
            if not self._silent:
                self.user_message.emit(f"Update failed: {exc}")
            self.completed.emit(False)
            return
        self.completed.emit(True)
