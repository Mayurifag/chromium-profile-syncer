#!/usr/bin/env python3
"""Cross-platform PyInstaller build script for chromium-profile-syncer.

Usage:
    uv run python build.py           # build only
    uv run python build.py --install # build + strip Gatekeeper + install to /usr/local/bin
"""

import shutil
import subprocess
import sys
from pathlib import Path

INSTALL_PATH = Path.home() / ".local" / "bin" / "chromium-profile-syncer"


def build() -> Path:
    cmd = [
        "uv",
        "run",
        "pyinstaller",
        "--onefile",
        "--windowed",
        "--name",
        "chromium-profile-syncer",
        "--hidden-import",
        "psutil",
        "--hidden-import",
        "PySide6.QtSvg",
        "src/main.py",
    ]

    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)

    candidates = list(Path("dist").glob("chromium-profile-syncer*"))
    if not candidates:
        print("Build completed but executable not found in dist/")
        sys.exit(1)

    binary = candidates[0]
    print(f"\nBuild successful: {binary.resolve()}")
    return binary


def install(binary: Path) -> None:
    if sys.platform == "darwin":
        print("Stripping Gatekeeper quarantine...")
        subprocess.run(["xattr", "-cr", str(binary)], check=True)

    INSTALL_PATH.parent.mkdir(parents=True, exist_ok=True)
    print(f"Installing to {INSTALL_PATH}...")
    shutil.copy2(binary, INSTALL_PATH)
    INSTALL_PATH.chmod(0o755)
    print(f"Installed. Run with: chromium-profile-syncer")


def main() -> None:
    do_install = "--install" in sys.argv
    binary = build()
    if do_install:
        install(binary)


if __name__ == "__main__":
    main()
