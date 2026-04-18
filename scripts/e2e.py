#!/usr/bin/env python
"""E2E: backup Helium Default → nuke Thorium → restore → launch for inspection."""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import time

from src import config
from src.browsers.helium import Helium
from src.browsers.thorium import Thorium
from src.sync.archive import ARCHIVE_NAME

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

CLI = [sys.executable, "-m", "src.main"]


def _run(args: list[str]) -> None:
    subprocess.run([*CLI, *args], check=True)


def _sep(label: str) -> None:
    print(f"\n── {label} {'─' * max(0, 55 - len(label))}")


def _kill(exe_name: str) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", exe_name], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", exe_name], capture_output=True)


def main() -> None:
    helium = Helium()
    thorium = Thorium()

    sync_folder = config.get_sync_folder()
    if sync_folder is None:
        sys.exit("ERROR: sync_folder not configured — run the app first")

    # ── 1. Backup Helium Default → current.tar ────────────────────────────
    _sep("1/5  Backup Helium → current.tar")
    if helium.is_running():
        sys.exit("ERROR: Helium is running — close it first")
    _run(["--sync", "--browser", "Helium", "--direction", "push"])

    # ── 2. Kill + nuke Thorium profile ────────────────────────────────────
    _sep("2/5  Nuke Thorium profile")
    thorium_root = thorium.profile_root()
    if thorium_root is None:
        sys.exit("ERROR: cannot determine Thorium profile root")

    _kill("thorium.exe" if sys.platform == "win32" else "thorium")
    time.sleep(1)

    if thorium_root.exists():
        shutil.rmtree(thorium_root)
        log.info("Deleted %s", thorium_root)
    else:
        log.info("Thorium profile already absent")

    # ── 3. Launch Thorium briefly to seed fresh profile ───────────────────
    _sep("3/5  Launch Thorium → fresh profile (~6s)")
    exe = thorium.executable()
    if exe is None:
        sys.exit("ERROR: Thorium executable not found")

    proc = subprocess.Popen([str(exe)])
    time.sleep(6)
    _kill("thorium.exe" if sys.platform == "win32" else "thorium")
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
    time.sleep(1)
    log.info("Thorium closed")

    # ── 4. Restore current.tar → Thorium ──────────────────────────────────
    _sep("4/5  Restore backup → Thorium")
    current_archive = sync_folder / ARCHIVE_NAME
    _run(["--restore-from", str(current_archive), "--browser", "Thorium"])

    # ── 5. Launch Thorium for inspection ──────────────────────────────────
    _sep("5/5  Launching Thorium — inspect, then close manually")
    subprocess.Popen([str(exe)])
    print("\nAll done. Check Thorium. Close it when finished.\n")


if __name__ == "__main__":
    main()
