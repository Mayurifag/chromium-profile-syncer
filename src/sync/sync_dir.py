from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

from src.operations import operation_lock
from src.sync import copy_file_atomic, replace_directory, write_text_if_changed

SYNC_DIR_NAME = "current"
_SKIP_NAMES = {"LOG", "LOG.old", "LOCK"}


class SnapshotConflictError(OSError):
    pass


def _files(directory: Path):
    if not directory.exists():
        return
    for path in sorted(directory.rglob("*")):
        if path.is_symlink():
            raise ValueError(f"Backup must not contain symbolic links: {path}")
        if path.name.startswith("._") or path.name in _SKIP_NAMES:
            continue
        if path.is_file() and path != directory / "metadata.json":
            yield path


def fingerprint(directory: Path) -> dict[str, str]:
    result = {}
    for path in _files(directory):
        with path.open("rb") as stream:
            result[path.relative_to(directory).as_posix()] = hashlib.file_digest(
                stream, "sha256"
            ).hexdigest()
    return result


def validate_snapshot(directory: Path) -> dict[str, str]:
    actual = fingerprint(directory)
    metadata = directory / "metadata.json"
    if metadata.exists():
        data = json.loads(metadata.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid snapshot metadata: {metadata}")
        expected = data.get("files")
        if expected is not None and actual != expected:
            raise SnapshotConflictError("Backup is incomplete or changed; let cloud sync finish.")
    return actual


def mark_changed(directory: Path) -> None:
    metadata = {"revision": uuid.uuid4().hex, "files": fingerprint(directory)}
    write_text_if_changed(directory / "metadata.json", json.dumps(metadata, sort_keys=True))


def write_if_changed(source: Path, target: Path) -> bool:
    if target.exists() and source.read_bytes() == target.read_bytes():
        return False
    copy_file_atomic(source, target)
    return True


def seed_work_dir(current: Path, work: Path) -> dict[str, str]:
    baseline = validate_snapshot(current)
    for path in _files(current):
        destination = work / path.relative_to(current)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, destination)
    if fingerprint(current) != baseline or fingerprint(work) != baseline:
        raise SnapshotConflictError(
            "Backup changed while reading it. Retry after cloud sync finishes."
        )
    return baseline


def merge_to_sync_dir(
    source: Path, current: Path, expected_state: dict[str, str] | None = None
) -> bool:
    current.parent.mkdir(parents=True, exist_ok=True)
    baseline = validate_snapshot(current)
    if expected_state is not None and baseline != expected_state:
        raise SnapshotConflictError(
            "Backup changed during this operation. Retry to merge the change."
        )
    if fingerprint(source) == baseline and (current / "metadata.json").exists():
        return False
    with tempfile.TemporaryDirectory(
        prefix=f".{current.name}-publish-", dir=current.parent
    ) as temp:
        staging = Path(temp) / "snapshot"
        staging.mkdir()
        for path in _files(source):
            destination = staging / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, destination)
        if fingerprint(staging) != fingerprint(source):
            raise SnapshotConflictError("Source changed while publishing the backup.")
        mark_changed(staging)
        if fingerprint(current) != baseline:
            raise SnapshotConflictError("Backup changed while publishing it. Retry the operation.")
        replace_directory(staging, current)
    return True


@contextmanager
def edit_snapshot(current: Path):
    with operation_lock(), tempfile.TemporaryDirectory(prefix="cps-edit-") as temp:
        work = Path(temp)
        baseline = seed_work_dir(current, work)
        yield work
        merge_to_sync_dir(work, current, baseline)
