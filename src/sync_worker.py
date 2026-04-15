from __future__ import annotations

import logging
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

import src.config as _config
from src.sync.archive import pack_to_archive
from src.sync_engine import SyncEngine

logger = logging.getLogger(__name__)


class _InitialUploadWorker(QThread):
    step = Signal(str)
    done = Signal()

    def __init__(self, src: Path, folder: Path) -> None:
        super().__init__()
        self._src = src
        self._folder = folder

    def run(self) -> None:
        engine = SyncEngine(self._folder)
        work_dir = Path(tempfile.mkdtemp(prefix="cps-upload-"))
        try:
            engine.sync_browser_profile(
                self._src, work_dir, direction="push",
                on_progress=lambda desc: self.step.emit(desc),
            )
            self.step.emit("Packing archive...")
            pack_to_archive(work_dir, self._folder / "current.tar")
        finally:
            shutil.rmtree(work_dir)
        self.done.emit()


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
        self._current_profile: tuple[str, str, str] | None = None
        self._profile_count = 0
        self._profile_start = 0.0

    def run(self) -> None:
        logger.info("SyncWorker: sync started")
        try:
            def _progress_handler(desc: str) -> None:
                self.progress.emit(desc)
                if "/" not in desc:
                    return
                browser, profile = desc.split("/", 1)
                if (
                    self._current_profile is None
                    or self._current_profile[0] != browser
                    or self._current_profile[1] != profile
                ):
                    directions = _config.get_profile_directions()
                    direction = directions.get(browser, {}).get(profile, "both")
                    direction_label = {"push": "TO", "pull": "FROM", "both": "FROM/TO"}.get(
                        direction, direction
                    )
                    self._current_profile = (browser, profile, direction_label)
                    self._profile_count = 0
                    self._profile_start = datetime.now().timestamp()

                self._profile_count += 1
                elapsed = datetime.now().timestamp() - self._profile_start
                last_emit = getattr(self, "_last_emit", 0)
                if self._profile_count % 10 == 0 or elapsed - last_emit > 1.0:
                    self.profile_progress.emit(
                        browser, profile, self._current_profile[2],
                        self._profile_count, elapsed,
                    )
                    self._last_emit = elapsed

            result = self._engine.sync_all(
                only_browser=self._only_browser,
                only_profile=self._only_profile,
                force_direction=self._force_direction,
                on_progress=_progress_handler,
            )
            ts = datetime.now(tz=UTC).isoformat()
            is_first_sync = result.get("is_first_sync", False)
            skipped_running = result.get("skipped_running", [])
            logger.info("SyncWorker: sync finished at %s", ts)
            self.finished.emit(ts, is_first_sync, skipped_running, self._only_browser or "")
        except Exception as exc:
            logger.exception("SyncWorker: sync error")
            self.error.emit(str(exc))
        finally:
            self._current_profile = None
