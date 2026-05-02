from __future__ import annotations

import logging
import re
import shutil

from PySide6.QtCore import QObject, QProcess, Signal

logger = logging.getLogger(__name__)

HELIUM_PACKAGE_ID = "ImputNet.Helium"


def is_winget_available() -> bool:
    return shutil.which("winget") is not None


def _parse_versions(output: str) -> tuple[str, str]:
    pattern = re.compile(rf"{re.escape(HELIUM_PACKAGE_ID)}\s+(.+)$", re.IGNORECASE | re.MULTILINE)
    match = pattern.search(output)
    if not match:
        return "", ""
    parts = match.group(1).split()
    if not parts:
        return "", ""
    installed = parts[0]
    available = parts[1] if len(parts) >= 3 else ""
    return installed, available


class WingetManager(QObject):
    detected = Signal(bool, str, str)
    upgrade_finished = Signal(bool, str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._is_managed: bool = False
        self._installed_version: str = ""
        self._available_version: str = ""
        self._detect_proc: QProcess | None = None
        self._upgrade_proc: QProcess | None = None

    @property
    def is_managed(self) -> bool:
        return self._is_managed

    @property
    def installed_version(self) -> str:
        return self._installed_version

    @property
    def available_version(self) -> str:
        return self._available_version

    def detect(self) -> None:
        if not is_winget_available():
            self._is_managed = False
            self._installed_version = ""
            self._available_version = ""
            self.detected.emit(False, "", "")
            return
        if self._detect_proc is not None and (
            self._detect_proc.state() != QProcess.ProcessState.NotRunning
        ):
            return
        proc = QProcess(self)
        proc.setProgram("winget")
        proc.setArguments([
            "list", "--id", HELIUM_PACKAGE_ID, "--exact",
            "--accept-source-agreements", "--disable-interactivity",
        ])
        proc.finished.connect(self._on_detect_finished)
        self._detect_proc = proc
        proc.start()

    def _on_detect_finished(self, exit_code: int, _exit_status) -> None:
        proc = self._detect_proc
        if proc is None:
            return
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        managed = exit_code == 0 and HELIUM_PACKAGE_ID.lower() in out.lower()
        installed, available = _parse_versions(out) if managed else ("", "")
        self._is_managed = managed
        self._installed_version = installed
        self._available_version = available
        logger.info(
            "winget Helium detection: managed=%s installed=%s available=%s (exit=%d)",
            managed, installed or "-", available or "-", exit_code,
        )
        self.detected.emit(managed, installed, available)

    def upgrade(self) -> None:
        if self._upgrade_proc is not None and (
            self._upgrade_proc.state() != QProcess.ProcessState.NotRunning
        ):
            logger.debug("winget upgrade already running — skipping")
            return
        proc = QProcess(self)
        proc.setProgram("winget")
        proc.setArguments([
            "upgrade", "--id", HELIUM_PACKAGE_ID, "--exact", "--silent",
            "--accept-source-agreements", "--accept-package-agreements",
            "--disable-interactivity",
        ])
        proc.finished.connect(self._on_upgrade_finished)
        self._upgrade_proc = proc
        logger.info("winget upgrade Helium: starting")
        proc.start()

    def _on_upgrade_finished(self, exit_code: int, _exit_status) -> None:
        proc = self._upgrade_proc
        if proc is None:
            return
        out = bytes(proc.readAllStandardOutput()).decode("utf-8", errors="replace")
        err = bytes(proc.readAllStandardError()).decode("utf-8", errors="replace")
        no_upgrade = (
            "No applicable upgrade" in out
            or "No installed package" in out
            or "No newer version" in out
        )
        if exit_code == 0 and not no_upgrade:
            logger.info("winget upgrade Helium: success")
            self.upgrade_finished.emit(True, "Helium updated")
        elif no_upgrade:
            logger.info("winget upgrade Helium: already up to date")
        else:
            logger.warning(
                "winget upgrade Helium failed: exit=%d stderr=%s",
                exit_code, err.strip()[:200] or out.strip()[:200],
            )
            self.upgrade_finished.emit(False, "Helium update failed — check log")
