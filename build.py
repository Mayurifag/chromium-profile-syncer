#!/usr/bin/env python3
"""Cross-platform PyInstaller build script for chromium-profile-syncer.

Usage:
    uv run python build.py           # build only
    uv run python build.py --install # build .app + strip Gatekeeper + install to ~/Applications
"""

import shutil
import subprocess
import sys
import time
from pathlib import Path

APP_NAME = "chromium-profile-syncer"
INSTALL_DIR = Path.home() / "Applications"
INSTALL_PATH = INSTALL_DIR / f"{APP_NAME}.app"


def build() -> Path:
    cmd = [
        "uv",
        "run",
        "pyinstaller",
        "-y",
        "--windowed",
        "--name",
        APP_NAME,
        "--hidden-import",
        "psutil",
        "--hidden-import",
        "PySide6.QtSvg",
        "src/main.py",
    ]

    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

    if sys.platform == "darwin":
        app_path = Path("dist") / f"{APP_NAME}.app"
        if not app_path.exists():
            print("Build completed but .app bundle not found in dist/")
            sys.exit(1)
        print(f"\nBuild successful: {app_path.resolve()}")
        return app_path

    # Non-macOS fallback: single binary
    candidates = list(Path("dist").glob(f"{APP_NAME}*"))
    candidates = [c for c in candidates if c.is_file()]
    if not candidates:
        print("Build completed but executable not found in dist/")
        sys.exit(1)
    binary = candidates[0]
    print(f"\nBuild successful: {binary.resolve()}")
    return binary


def _kill_running() -> None:
    """Kill any running instance via the lock file, then remove the lock."""
    import os
    import signal

    config_dir = Path.home() / ".config" / APP_NAME
    lock_file = config_dir / "app.lock"
    try:
        parts = lock_file.read_text(encoding="utf-8").strip().split(":", 1)
        pid = int(parts[0])
        os.kill(pid, signal.SIGTERM)
        print(f"Terminated running instance (pid={pid})")
        time.sleep(1)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError):
        pass


def install(artifact: Path) -> None:
    _kill_running()

    if sys.platform == "darwin":
        print("Stripping Gatekeeper quarantine...")
        subprocess.run(["xattr", "-cr", str(artifact)], check=True)

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    if INSTALL_PATH.exists():
        print(f"Removing existing {INSTALL_PATH}...")
        shutil.rmtree(INSTALL_PATH) if INSTALL_PATH.is_dir() else INSTALL_PATH.unlink()

    print(f"Installing to {INSTALL_PATH}...")
    if artifact.is_dir():
        shutil.copytree(artifact, INSTALL_PATH)
    else:
        shutil.copy2(artifact, INSTALL_PATH)
        INSTALL_PATH.chmod(0o755)

    print(f"Installed: {INSTALL_PATH}")

    print("Launching...")
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(INSTALL_PATH)])
    else:
        subprocess.Popen([str(INSTALL_PATH)])


def main() -> None:
    do_install = "--install" in sys.argv
    artifact = build()
    if do_install:
        install(artifact)


if __name__ == "__main__":
    main()
