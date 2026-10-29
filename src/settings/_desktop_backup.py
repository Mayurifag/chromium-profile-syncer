from __future__ import annotations

import tarfile
import tempfile
from pathlib import Path, PurePosixPath

from PySide6.QtCore import QStandardPaths

from src.operations import locked
from src.sync import replace_directory
from src.sync.sync_dir import mark_changed, seed_work_dir, validate_snapshot

_BACKUP_FILENAME = "chromium-profile-syncer-backup.tar.gz"


def desktop_dir() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DesktopLocation)
    if not location:
        raise OSError("The system could not locate your Desktop directory")
    return Path(location)


def desktop_backup_path() -> Path:
    return desktop_dir() / _BACKUP_FILENAME


@locked
def create(current_dir: Path) -> Path:
    if not current_dir.is_dir():
        raise FileNotFoundError(current_dir)
    validate_snapshot(current_dir)
    target = desktop_backup_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cps-backup-", dir=target.parent) as temp:
        snapshot = Path(temp) / current_dir.name
        seed_work_dir(current_dir, snapshot)
        mark_changed(snapshot)
        staging = Path(temp) / target.name
        with tarfile.open(staging, "w:gz") as archive:
            archive.add(snapshot, arcname=current_dir.name)
        staging.replace(target)
    return target


@locked
def restore(target_current_dir: Path) -> None:
    source = desktop_backup_path()
    target_current_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".cps-restore-", dir=target_current_dir.parent) as temp:
        staging = Path(temp) / "extracted"
        staging.mkdir()
        with tarfile.open(source, "r:gz") as archive:
            members = archive.getmembers()
            if not members:
                raise ValueError("The backup archive is empty")
            for member in members:
                path = PurePosixPath(member.name)
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or "\\" in member.name
                    or ":" in member.name
                    or not (member.isfile() or member.isdir())
                ):
                    raise ValueError(f"Unsafe archive entry: {member.name}")
            archive.extractall(staging, members=members, filter="data")
        entries = list(staging.iterdir())
        snapshot = (
            staging / target_current_dir.name
            if len(entries) == 1
            and (entries[0].name == target_current_dir.name and entries[0].is_dir())
            else staging
        )
        if not any(snapshot.iterdir()):
            raise ValueError("The backup contains no snapshot data")
        validate_snapshot(snapshot)
        mark_changed(snapshot)
        replace_directory(snapshot, target_current_dir)


@locked
def delete() -> None:
    desktop_backup_path().unlink(missing_ok=True)
