from __future__ import annotations

import shutil
import subprocess
import tarfile
from pathlib import Path

_BACKUP_FILENAME = "chromium-profile-syncer-backup.tar.gz"


def desktop_dir() -> Path:
    try:
        out = subprocess.run(
            ["xdg-user-dir", "DESKTOP"],
            capture_output=True,
            text=True,
            timeout=2,
            check=True,
        ).stdout.strip()
        if out:
            p = Path(out)
            if p.is_dir():
                return p
    except (FileNotFoundError, subprocess.SubprocessError):
        pass
    return Path.home() / "Desktop"


def desktop_backup_path() -> Path:
    return desktop_dir() / _BACKUP_FILENAME


def create(current_dir: Path) -> Path:
    target = desktop_backup_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(current_dir, arcname=current_dir.name)
    tmp.replace(target)
    return target


def restore(target_current_dir: Path) -> None:
    src = desktop_backup_path()
    if not src.is_file():
        raise FileNotFoundError(src)
    parent = target_current_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    if target_current_dir.exists():
        shutil.rmtree(target_current_dir)
    with tarfile.open(src, "r:gz") as tar:
        members = tar.getmembers()
        top_levels = {m.name.split("/", 1)[0] for m in members}
        if len(top_levels) == 1 and next(iter(top_levels)) == target_current_dir.name:
            tar.extractall(parent, filter="data")
        else:
            tar.extractall(target_current_dir, filter="data")


def delete() -> None:
    desktop_backup_path().unlink(missing_ok=True)
