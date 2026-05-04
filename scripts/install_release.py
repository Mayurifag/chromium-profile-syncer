#!/usr/bin/env python3
"""Download latest GitHub release asset, kill running app, install, launch."""

import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build import APP_NAME, install  # noqa: E402

REPO = "Mayurifag/chromium-profile-syncer"

ASSETS = {
    "linux": "chromium-profile-syncer-linux",
    "win32": "chromium-profile-syncer-windows.zip",
    "darwin": "chromium-profile-syncer-macos.zip",
}


def _asset_name() -> str:
    if sys.platform.startswith("linux"):
        return ASSETS["linux"]
    if sys.platform == "win32":
        return ASSETS["win32"]
    if sys.platform == "darwin":
        return ASSETS["darwin"]
    print(f"Unsupported platform: {sys.platform}")
    sys.exit(1)


def _download(url: str, dest: Path) -> None:
    print(f"Downloading {url}")
    req = urllib.request.Request(url, headers={"User-Agent": "install-release"})
    with urllib.request.urlopen(req) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _prepare_artifact(downloaded: Path, work_dir: Path) -> Path:
    if downloaded.suffix != ".zip":
        downloaded.chmod(0o755)
        return downloaded

    extract_dir = work_dir / "extracted"
    extract_dir.mkdir()
    with zipfile.ZipFile(downloaded) as zf:
        zf.extractall(extract_dir)

    if sys.platform == "darwin":
        app = next(extract_dir.glob("*.app"), None)
        if app is None:
            print("No .app bundle in zip")
            sys.exit(1)
        return app

    bundle = work_dir / APP_NAME
    bundle.mkdir()
    for item in extract_dir.iterdir():
        shutil.move(str(item), bundle / item.name)
    return bundle


def main() -> None:
    asset = _asset_name()
    url = f"https://github.com/{REPO}/releases/latest/download/{asset}"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        downloaded = work / asset
        _download(url, downloaded)
        artifact = _prepare_artifact(downloaded, work)
        install(artifact)


if __name__ == "__main__":
    main()
