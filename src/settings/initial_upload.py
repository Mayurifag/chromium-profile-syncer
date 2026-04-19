from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QDialog, QLabel, QProgressBar, QVBoxLayout

from src.sync.sync_dir import SYNC_DIR_NAME, merge_to_sync_dir
from src.sync_engine import SyncEngine


class _InitialUploadWorker(QThread):
    step = Signal(str)
    done = Signal()

    def __init__(
        self, src: Path, folder: Path, ext_id_aliases: dict[str, str] | None = None
    ) -> None:
        super().__init__()
        self._src = src
        self._folder = folder
        self._ext_id_aliases = ext_id_aliases

    def run(self) -> None:
        engine = SyncEngine(self._folder)
        work_dir = Path(tempfile.mkdtemp(prefix="cps-upload-"))
        try:
            engine.sync_browser_profile(
                self._src, work_dir, direction="push",
                on_progress=self.step.emit,
                ext_id_aliases=self._ext_id_aliases,
            )
            self.step.emit("Syncing to folder...")
            current_dir = self._folder / SYNC_DIR_NAME
            merge_to_sync_dir(work_dir, current_dir)
            (current_dir / "metadata.json").write_text("{}", encoding="utf-8")
        finally:
            shutil.rmtree(work_dir)
        self.done.emit()


class InitialUploadDialog(QDialog):
    upload_done = Signal(str, str, int, float)  # browser_name, profile_name, count, elapsed

    def __init__(
        self,
        parent=None,
        *,
        profile_path: Path,
        folder: Path,
        browser_name: str,
        profile_name: str,
        ext_id_aliases: dict[str, str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Uploading Profile")
        self.setMinimumWidth(420)
        self.setWindowFlags(self.windowFlags() & ~0x00000008)

        layout = QVBoxLayout(self)
        self._op_label = QLabel("Starting…")
        self._op_label.setWordWrap(True)
        layout.addWidget(self._op_label)

        bar = QProgressBar()
        bar.setRange(0, 0)
        layout.addWidget(bar)

        self._stats_label = QLabel("")
        layout.addWidget(self._stats_label)

        self._browser_name = browser_name
        self._profile_name = profile_name
        self._count = 0
        self._start = 0.0

        self._worker = _InitialUploadWorker(profile_path, folder, ext_id_aliases)
        self._worker.step.connect(self._on_step)
        self._worker.done.connect(self._on_done)

    def start(self) -> None:
        self._start = time.monotonic()
        self._worker.start()
        self.show()

    def _on_step(self, description: str) -> None:
        self._count += 1
        elapsed = time.monotonic() - self._start
        self._op_label.setText(f"Copying: <b>{description}</b>")
        rate = self._count / elapsed if elapsed > 0.1 else 0
        self._stats_label.setText(
            f"{self._count} items copied • {elapsed:.0f}s elapsed"
            + (f" • ~{rate:.1f} items/s" if rate > 0 else "")
        )

    def _on_done(self) -> None:
        elapsed = time.monotonic() - self._start
        self.close()
        self.upload_done.emit(self._browser_name, self._profile_name, self._count, elapsed)
