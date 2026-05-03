from __future__ import annotations

import filecmp
import logging
import shutil
from pathlib import Path

_LOG = logging.getLogger(__name__)

SYNC_DIR_NAME = "current"
_SKIP_NAMES = {"LOG", "LOG.old", "LOCK"}
# Root-level files written directly to current_dir (not via work_dir). Never
# delete them during merge_to_sync_dir — their absence from work_dir is by design.
_PRESERVE_ROOT_FILES = {
    "metadata.json", "search_shortcuts.json", "typed_urls.json", "browser_flags.json",
}


def write_if_changed(src: Path, dst: Path) -> bool:
    if dst.exists() and filecmp.cmp(src, dst, shallow=False):
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dst)
    return True


def seed_work_dir(current: Path, work: Path) -> None:
    if not current.exists():
        return
    for src_path in current.rglob("*"):
        if not src_path.is_file():
            continue
        if src_path.name.startswith("._") or src_path.name in _SKIP_NAMES:
            continue
        rel = src_path.relative_to(current)
        dst = work / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_path, dst)


def merge_to_sync_dir(src: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)

    src_files: set[Path] = set()
    for src_path in src.rglob("*"):
        if src_path.name.startswith("._") or src_path.name in _SKIP_NAMES:
            continue
        rel = src_path.relative_to(src)
        if src_path.is_file():
            src_files.add(rel)
            write_if_changed(src_path, dst / rel)

    for dst_path in list(dst.rglob("*")):
        if not dst_path.is_file():
            continue
        rel = dst_path.relative_to(dst)
        if rel.parent == Path(".") and rel.name in _PRESERVE_ROOT_FILES:
            continue
        if rel not in src_files:
            dst_path.unlink()
            _LOG.debug("Deleted stale: %s", rel)

    for dst_dir in sorted(dst.rglob("*"), reverse=True):
        if dst_dir.is_dir() and not any(dst_dir.iterdir()):
            dst_dir.rmdir()
