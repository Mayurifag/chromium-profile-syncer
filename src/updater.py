from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

REPO = "Mayurifag/chromium-profile-syncer"
LATEST_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
APP_NAME = "chromium-profile-syncer"
STAGING_DIR_NAME = f"{APP_NAME}-update"


class UpdateCheckError(Exception):
    pass


def _local_sha() -> str:
    try:
        from src._build_info import BUILD_SHA
    except ImportError:
        return ""
    return BUILD_SHA


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _asset_name() -> str:
    if sys.platform == "win32":
        return f"{APP_NAME}-windows.zip"
    if sys.platform == "darwin":
        return f"{APP_NAME}-macos.zip"
    return f"{APP_NAME}-linux"


def _staging_dir() -> Path:
    return Path(tempfile.gettempdir()) / STAGING_DIR_NAME


def cleanup_staging() -> None:
    d = _staging_dir()
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


def _fetch_latest() -> dict:
    """Fetch latest release JSON. Raises UpdateCheckError on failure."""
    try:
        req = urllib.request.Request(
            LATEST_URL,
            headers={"Accept": "application/vnd.github+json", "User-Agent": APP_NAME},
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as exc:
        raise UpdateCheckError(f"fetch latest failed: {exc}") from exc


def check_for_update() -> tuple[str, str, str] | None:
    """Return (target_sha, asset_url, sha256_url) if update available, else None.
    Raises UpdateCheckError on network/API failure."""
    if not _is_frozen():
        return None
    local = _local_sha()
    if not local:
        logger.debug("update: no BUILD_SHA — skipping")
        return None
    data = _fetch_latest()
    remote = data.get("target_commitish", "")
    if not remote or remote == local:
        return None
    asset_name = _asset_name()
    sha_name = f"{asset_name}.sha256"
    asset_url = sha_url = ""
    for asset in data.get("assets", []):
        name = asset.get("name")
        if name == asset_name:
            asset_url = asset["browser_download_url"]
        elif name == sha_name:
            sha_url = asset["browser_download_url"]
    if not asset_url:
        raise UpdateCheckError(f"asset {asset_name} missing from release {data.get('tag_name')}")
    if not sha_url:
        raise UpdateCheckError(f"checksum {sha_name} missing from release {data.get('tag_name')}")
    return remote, asset_url, sha_url


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(req, timeout=120) as r, dest.open("wb") as f:
        shutil.copyfileobj(r, f)


def _read_url(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8").strip()


def _verify_sha256(path: Path, expected: str) -> None:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    if digest != expected.lower():
        raise RuntimeError(f"sha256 mismatch: got {digest}, expected {expected}")


def _spawn_detached(cmd: list[str]) -> None:
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(cmd, creationflags=flags, close_fds=True)
    else:
        subprocess.Popen(cmd, start_new_session=True, close_fds=True)


def _install_windows(zip_path: Path, staging: Path) -> None:
    extracted = staging / "extracted"
    extracted.mkdir()
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extracted)
    target_exe = Path(sys.executable).resolve()
    install_dir = target_exe.parent
    pid = os.getpid()
    bat = staging / "swap.bat"
    bat.write_text(
        f'''@echo off
:wait
timeout /t 1 /nobreak > nul
tasklist /FI "PID eq {pid}" 2>nul | find "{pid}" >nul
if not errorlevel 1 goto wait
robocopy "{extracted}" "{install_dir}" /MIR /IS /IT /R:20 /W:1 /NP /NJH /NJS > nul
if errorlevel 8 goto fail
start "" "{target_exe}" --tray
exit /b 0
:fail
echo robocopy failed with errorlevel %errorlevel% >> "{staging}\\update.log"
exit /b 1
''',
        encoding="utf-8",
    )
    _spawn_detached(["cmd.exe", "/c", str(bat)])


def _install_linux(binary_path: Path, staging: Path) -> None:
    target = Path(sys.executable).resolve()
    pid = os.getpid()
    sh = staging / "swap.sh"
    sh.write_text(
        f'''#!/bin/sh
while kill -0 {pid} 2>/dev/null; do sleep 1; done
mv -f "{binary_path}" "{target}"
chmod +x "{target}"
"{target}" --tray &
''',
        encoding="utf-8",
    )
    sh.chmod(0o755)
    _spawn_detached(["/bin/sh", str(sh)])


def _install_macos(zip_path: Path, staging: Path) -> None:
    extracted = staging / "extracted"
    extracted.mkdir()
    subprocess.run(["unzip", "-q", str(zip_path), "-d", str(extracted)], check=True)
    new_app = next(extracted.glob("*.app"), None)
    if new_app is None:
        raise RuntimeError("macOS asset has no .app bundle")
    exe = Path(sys.executable).resolve()
    target_app = exe.parents[2]
    if target_app.suffix != ".app":
        raise RuntimeError(f"unexpected executable layout: {exe}")
    pid = os.getpid()
    sh = staging / "swap.sh"
    sh.write_text(
        f'''#!/bin/sh
while kill -0 {pid} 2>/dev/null; do sleep 1; done
rm -rf "{target_app}"
mv "{new_app}" "{target_app}"
xattr -cr "{target_app}" 2>/dev/null
open "{target_app}" --args --tray
''',
        encoding="utf-8",
    )
    sh.chmod(0o755)
    _spawn_detached(["/bin/sh", str(sh)])


def install_update(asset_url: str, sha_url: str) -> None:
    """Download asset, verify sha256, spawn detached helper to swap + relaunch.
    Caller exits after."""
    cleanup_staging()
    staging = _staging_dir()
    staging.mkdir(parents=True)
    asset_path = staging / _asset_name()
    logger.info("update: downloading %s", asset_url)
    _download(asset_url, asset_path)
    expected = _read_url(sha_url).split()[0]
    logger.info("update: verifying sha256")
    _verify_sha256(asset_path, expected)
    logger.info("update: spawning swap helper from %s", staging)
    if sys.platform == "win32":
        _install_windows(asset_path, staging)
    elif sys.platform == "darwin":
        _install_macos(asset_path, staging)
    else:
        _install_linux(asset_path, staging)
