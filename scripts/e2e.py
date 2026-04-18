#!/usr/bin/env python
"""E2E: backup Helium Default → nuke Thorium → restore → launch for inspection."""
from __future__ import annotations

import logging
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from src import config
from src.browsers.helium import Helium
from src.browsers.thorium import Thorium
from src.sync import archive as _archive
from src.sync import extensions as _extensions
from src.sync_engine import SyncEngine

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)


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
    helium_profiles = helium.discover_profiles()
    if not helium_profiles:
        sys.exit("ERROR: Helium has no profiles")
    profile_path = next((p for p in helium_profiles if p.name == "Default"), helium_profiles[0])
    log.info("Source profile: %s", profile_path)

    if helium.is_running():
        sys.exit("ERROR: Helium is running — close it first")

    current_archive = sync_folder / "current.tar"
    work_dir = Path(tempfile.mkdtemp(prefix="cps-e2e-bk-"))
    try:
        if current_archive.exists():
            log.info("Seeding work_dir from existing archive...")
            _archive.unpack_archive(current_archive, work_dir)
        engine = SyncEngine(sync_folder)
        engine.sync_browser_profile(
            profile_path, work_dir,
            direction="push",
            on_progress=lambda m: log.info("  push: %s", m),
        )
        if _archive.validate_archive_content(work_dir):
            _archive.pack_to_archive(work_dir, current_archive)
            log.info("Packed → %s", current_archive)
        else:
            sys.exit("ERROR: archive validation failed after push")
    finally:
        shutil.rmtree(work_dir)

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
    thorium_profiles = thorium.discover_profiles()
    if not thorium_profiles:
        sys.exit("ERROR: Thorium has no profiles after fresh launch")

    work_dir = Path(tempfile.mkdtemp(prefix="cps-e2e-rs-"))
    try:
        _archive.unpack_archive(current_archive, work_dir)
        restore_engine = SyncEngine(sync_folder)
        ungoogled_only = config.get_ungoogled_only_extensions()
        ext_restrictions = config.get_extension_browser_restrictions()

        for tp in thorium_profiles:
            log.info("Restoring → %s", tp)
            restore_engine.restore_profile_from_backup(
                tp, work_dir,
                browser=thorium,
                on_progress=lambda m: log.info("  restore: %s", m),
            )
            _extensions.install_external_extensions(
                work_dir, thorium,
                ungoogled_only_ext_ids=ungoogled_only,
                browser_restrictions=ext_restrictions,
            )
    finally:
        shutil.rmtree(work_dir)

    # ── 5. Launch Thorium for inspection ──────────────────────────────────
    _sep("5/5  Launching Thorium — inspect, then close manually")
    subprocess.Popen([str(exe)])
    print("\nAll done. Check Thorium. Close it when finished.\n")


if __name__ == "__main__":
    main()
