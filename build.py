#!/usr/bin/env python3
"""Cross-platform PyInstaller build script for chromium-profile-syncer.

Usage:
    uv run python build.py           # build only
    uv run python build.py --install # build + install to platform-specific location

Install locations:
    macOS:   ~/Applications/chromium-profile-syncer.app
    Windows: %LOCALAPPDATA%\\Programs\\chromium-profile-syncer\\chromium-profile-syncer.exe
    Linux:   ~/.local/bin/chromium-profile-syncer
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:
    import psutil
except ImportError:
    print("Warning: psutil not installed, cannot kill running instance")
    psutil = None

APP_NAME = "chromium-profile-syncer"


def _get_install_dir() -> Path:
    """Return platform-specific install directory."""
    if sys.platform == "darwin":
        return Path.home() / "Applications"
    elif sys.platform == "win32":
        # Windows: %LOCALAPPDATA%\Programs
        localappdata = Path.home() / "AppData" / "Local"
        return localappdata / "Programs"
    else:
        # Linux: ~/.local/bin
        return Path.home() / ".local" / "bin"


def _get_install_path() -> Path:
    """Return full install path including app name."""
    install_dir = _get_install_dir()
    if sys.platform == "darwin":
        return install_dir / f"{APP_NAME}.app"
    elif sys.platform == "win32":
        return install_dir / APP_NAME / f"{APP_NAME}.exe"
    else:
        return install_dir / APP_NAME


INSTALL_DIR = _get_install_dir()
INSTALL_PATH = _get_install_path()


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

    # Non-macOS: check for onedir bundle first (Windows default), then single binary
    dist_dir = Path("dist") / APP_NAME
    if dist_dir.is_dir():
        print(f"\nBuild successful: {dist_dir.resolve()}")
        return dist_dir

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
    if psutil is None:
        return

    # Use proper config dir (cross-platform)
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        config_dir = Path(appdata) / APP_NAME if appdata else Path.home() / "AppData" / "Roaming" / APP_NAME
    else:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        config_dir = Path(xdg_config) / APP_NAME if xdg_config else Path.home() / ".config" / APP_NAME

    lock_file = config_dir / "app.lock"
    try:
        parts = lock_file.read_text(encoding="utf-8").strip().split(":", 1)
        pid = int(parts[0])
        proc = psutil.Process(pid)
        proc.terminate()
        print(f"Terminated running instance (pid={pid})")
        time.sleep(1)
    except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError, OSError, psutil.NoSuchProcess):
        pass


def install(artifact: Path) -> None:
    _kill_running()

    if sys.platform == "darwin":
        print("Stripping Gatekeeper quarantine...")
        subprocess.run(["xattr", "-cr", str(artifact)], check=True)

    INSTALL_DIR.mkdir(parents=True, exist_ok=True)

    print(f"Installing to {INSTALL_PATH}...")
    if artifact.is_dir():
        if sys.platform == "darwin":
            # macOS .app bundle: install as-is
            if INSTALL_PATH.exists():
                print(f"Removing existing {INSTALL_PATH}...")
                shutil.rmtree(INSTALL_PATH)
            shutil.copytree(artifact, INSTALL_PATH)
        else:
            # Windows onedir: copy whole bundle dir to INSTALL_PATH.parent
            install_target = INSTALL_PATH.parent
            if install_target.exists():
                print(f"Removing existing {install_target}...")
                shutil.rmtree(install_target)
            shutil.copytree(artifact, install_target)
    else:
        # Linux single executable
        if INSTALL_PATH.exists():
            print(f"Removing existing {INSTALL_PATH}...")
            INSTALL_PATH.unlink()
        shutil.copy2(artifact, INSTALL_PATH)
        INSTALL_PATH.chmod(0o755)

    print(f"Installed: {INSTALL_PATH}")

    # Add to PATH on Linux if not already there
    if sys.platform not in ("darwin", "win32"):
        bin_dir = INSTALL_DIR
        _add_to_path_if_needed(bin_dir)

    print("Launching...")
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(INSTALL_PATH)])
    elif sys.platform == "win32":
        subprocess.Popen([str(INSTALL_PATH)], creationflags=subprocess.DETACHED_PROCESS)
    else:
        subprocess.Popen([str(INSTALL_PATH)])


def _add_to_path_if_needed(bin_dir: Path) -> None:
    """Check if bin_dir is in PATH, suggest adding if not (Linux only)."""
    path_env = os.environ.get("PATH", "")
    if str(bin_dir) not in path_env.split(os.pathsep):
        shell_rc = Path.home() / ".bashrc"
        if not shell_rc.exists():
            shell_rc = Path.home() / ".zshrc"
        print(f"\nNote: {bin_dir} is not in your PATH.")
        print(f"Add this line to your {shell_rc.name}:")
        print(f'  export PATH="{bin_dir}:$PATH"')


def main() -> None:
    do_install = "--install" in sys.argv
    artifact = build()
    if do_install:
        install(artifact)


if __name__ == "__main__":
    main()
