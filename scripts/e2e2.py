#!/usr/bin/env python
"""E2E2: backup Helium Default → create fresh Profile 1 → restore → launch for inspection."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
from pathlib import Path

from src import config
from src.browsers.helium import Helium
from src.sync.archive import ARCHIVE_NAME

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger(__name__)

CLI = [sys.executable, "-m", "src.main"]
CLI_PY = [sys.executable, str(Path(__file__).parent.parent / "cli.py")]
PROFILE_DIR = "Profile 1"


def _run(args: list[str]) -> None:
    subprocess.run([*CLI, *args], check=True)


def _run_cli(args: list[str]) -> None:
    subprocess.run([*CLI_PY, *args], check=True)


def _sep(label: str) -> None:
    print(f"\n── {label} {'─' * max(0, 55 - len(label))}")


def _kill(exe_name: str) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/F", "/IM", exe_name], capture_output=True)
    else:
        subprocess.run(["pkill", "-f", exe_name], capture_output=True)


def _graceful_kill(proc: subprocess.Popen, exe_name: str, timeout: int = 10) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", exe_name], capture_output=True)
    else:
        subprocess.run(["pkill", "-TERM", "-f", exe_name], capture_output=True)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(exe_name)
        proc.wait(timeout=5)


def _patch_profile_name(prefs_path: Path, name: str) -> None:
    if not prefs_path.exists():
        return
    data = json.loads(prefs_path.read_text(encoding="utf-8"))
    data.setdefault("profile", {})["name"] = name
    prefs_path.write_text(json.dumps(data), encoding="utf-8")
    log.info("Profile name set to %r", name)


def clean(helium_root: Path, exe_name: str) -> None:
    _sep("Clean — removing Profile 1")
    _kill(exe_name)
    time.sleep(1)
    profile_1_path = helium_root / PROFILE_DIR
    if profile_1_path.exists():
        shutil.rmtree(profile_1_path)
        log.info("Deleted %s", profile_1_path)
    else:
        log.info("Profile 1 not found — nothing to clean")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clean", action="store_true", help="Delete Profile 1 and exit")
    args = parser.parse_args()

    helium = Helium()

    helium_root = helium.profile_root()
    if helium_root is None:
        sys.exit("ERROR: cannot determine Helium profile root")

    exe_name = "chrome.exe" if sys.platform == "win32" else "helium"

    if args.clean:
        clean(helium_root, exe_name)
        return

    sync_folder = config.get_sync_folder()
    if sync_folder is None:
        sys.exit("ERROR: sync_folder not configured — run the app first")

    exe = helium.executable()
    if exe is None:
        sys.exit("ERROR: Helium executable not found")

    # ── 1. Backup Helium Default → current.tar ────────────────────────────
    _sep("1/5  Backup Helium Default → current.tar")
    if helium.is_running():
        sys.exit("ERROR: Helium is running — close it first")
    _run(["--sync", "--browser", "Helium", "--profile", "Default", "--direction", "push"])

    if not (sync_folder / ARCHIVE_NAME).exists():
        sys.exit("ERROR: archive not created — is Helium/Default enabled in config?")

    # ── 2. Kill + nuke Profile 1 ──────────────────────────────────────────
    _sep("2/5  Nuke Helium Profile 1")
    _kill(exe_name)
    time.sleep(1)

    profile_1_path = helium_root / PROFILE_DIR
    if profile_1_path.exists():
        shutil.rmtree(profile_1_path)
        log.info("Deleted %s", profile_1_path)
    else:
        log.info("Profile 1 already absent")

    # ── 3. Launch Helium briefly to seed fresh Profile 1 ──────────────────
    _sep("3/5  Launch Helium → fresh Profile 1 (~6s)")
    proc = subprocess.Popen([str(exe), f"--profile-directory={PROFILE_DIR}"])
    time.sleep(6)
    _graceful_kill(proc, exe_name)
    time.sleep(1)
    log.info("Helium closed")

    if not profile_1_path.exists():
        sys.exit("ERROR: Profile 1 was not created — Helium may have failed to launch")

    # ── 4. Restore current.tar → Profile 1 only ───────────────────────────
    _sep("4/5  Restore backup → Profile 1")
    _run_cli(["restore", "--browser", "Helium", "--profile", PROFILE_DIR])
    _patch_profile_name(profile_1_path / "Preferences", "E2E Test")

    # ── 5. Launch Helium on Profile 1 for inspection ──────────────────────
    _sep("5/5  Launching Helium (Profile 1) — inspect, then close manually")
    subprocess.Popen([str(exe), f"--profile-directory={PROFILE_DIR}"])
    print("\nAll done. Check Helium Profile 1. Close it when finished.\n")


if __name__ == "__main__":
    main()
