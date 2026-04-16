from __future__ import annotations

import logging
from datetime import UTC, datetime

from PySide6.QtCore import QThread, Signal

import src.config as _config
from src.sync_engine import SyncEngine

logger = logging.getLogger(__name__)

_DIRECTION_LABELS: dict[str, str] = {"push": "TO", "pull": "FROM", "both": "FROM/TO"}


class _ProfileProgressTracker:
    def __init__(self, worker: SyncWorker) -> None:
        self._worker = worker
        self.current: tuple[str, str, str] | None = None
        self.count = 0
        self.start = 0.0
        self._last_emit = 0.0

    def __call__(self, desc: str) -> None:
        self._worker.progress.emit(desc)
        if "/" not in desc:
            return
        browser, profile = desc.split("/", 1)
        if self.current is None or self.current[:2] != (browser, profile):
            direction = _config.get_profile_directions().get(browser, {}).get(profile, "both")
            label = _DIRECTION_LABELS.get(direction, direction)
            self.current = (browser, profile, label)
            self.count = 0
            self.start = datetime.now().timestamp()
        self.count += 1
        elapsed = datetime.now().timestamp() - self.start
        if self.count % 10 == 0 or elapsed - self._last_emit > 1.0:
            self._worker.profile_progress.emit(
                browser, profile, self.current[2], self.count, elapsed
            )
            self._last_emit = elapsed


class SyncWorker(QThread):
    finished = Signal(str, bool, list, str)  # ts, is_first_sync, skipped_running, triggered_browser
    error = Signal(str)
    progress = Signal(str)  # type: ignore[assignment]
    profile_progress = Signal(str, str, str, int, float)  # browser, profile, dir, count, elapsed

    def __init__(
        self,
        engine: SyncEngine,
        only_browser: str | None = None,
        only_profile: str | None = None,
        force_direction: str | None = None,
    ) -> None:
        super().__init__()
        self._engine = engine
        self._only_browser = only_browser
        self._only_profile = only_profile
        self._force_direction = force_direction

    def run(self) -> None:
        logger.info("SyncWorker: sync started")
        try:
            tracker = _ProfileProgressTracker(self)
            result = self._engine.sync_all(
                only_browser=self._only_browser,
                only_profile=self._only_profile,
                force_direction=self._force_direction,
                on_progress=tracker,
            )
            ts = datetime.now(tz=UTC).isoformat()
            is_first_sync = result.get("is_first_sync", False)
            skipped_running = result.get("skipped_running", [])
            logger.info("SyncWorker: sync finished at %s", ts)
            self.finished.emit(ts, is_first_sync, skipped_running, self._only_browser or "")
        except Exception as exc:
            logger.exception("SyncWorker: sync error")
            self.error.emit(str(exc))
