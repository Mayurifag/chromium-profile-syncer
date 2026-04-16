from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from src.sync import _noop

_LOG = logging.getLogger(__name__)


def dir_mtime(directory: Path) -> float:
    try:
        mtimes = [f.stat().st_mtime for f in directory.rglob("*") if f.is_file()]
    except OSError:
        return 0.0
    return max(mtimes) if mtimes else 0.0


def copy_atomic(
    src: Path,
    dst: Path,
    report: Callable[[str], None] = _noop,
    *,
    display_name: str | None = None,
) -> None:
    tmp = dst.parent / f"{dst.name}.tmp"
    try:
        if tmp.exists():
            shutil.rmtree(tmp)
        report(display_name or src.name)
        shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("._*"))
        if dst.exists():
            shutil.rmtree(dst)
        shutil.move(str(tmp), dst)
    except OSError:
        _LOG.exception("Atomic copy failed: %s → %s (dst untouched)", src, dst)


def sync_dir(
    profile_dir: Path,
    sync_path: Path,
    subpath: str,
    direction: str = "both",
    report: Callable[[str], None] = _noop,
    name_filter: Callable[[str], bool] | None = None,
) -> tuple[int, int]:
    profile_base = profile_dir / subpath
    sync_base = sync_path / subpath

    unit_names: set[str] = set()
    if profile_base.exists():
        unit_names.update(d.name for d in profile_base.iterdir() if d.is_dir())
    if sync_base.exists():
        unit_names.update(d.name for d in sync_base.iterdir() if d.is_dir())
    if name_filter is not None:
        unit_names = {n for n in unit_names if name_filter(n)}

    # Use lists for thread-safe accumulation (list.append is GIL-protected)
    synced_list: list[int] = []
    skipped_list: list[int] = []

    def _sync_unit(name: str) -> None:
        profile_unit = profile_base / name
        sync_unit = sync_base / name
        profile_mtime = dir_mtime(profile_unit) if profile_unit.exists() else 0.0
        sync_mtime = dir_mtime(sync_unit) if sync_unit.exists() else 0.0

        if profile_mtime == sync_mtime:
            skipped_list.append(1)
            return

        if direction == "push":
            if profile_mtime > sync_mtime:
                sync_base.mkdir(parents=True, exist_ok=True)
                copy_atomic(profile_unit, sync_unit, report)
                synced_list.append(1)
            else:
                skipped_list.append(1)
        elif direction == "pull":
            if sync_mtime > profile_mtime:
                profile_base.mkdir(parents=True, exist_ok=True)
                copy_atomic(sync_unit, profile_unit, report)
                synced_list.append(1)
            else:
                skipped_list.append(1)
        else:
            if profile_mtime > sync_mtime:
                sync_base.mkdir(parents=True, exist_ok=True)
                copy_atomic(profile_unit, sync_unit, report)
                synced_list.append(1)
            else:
                profile_base.mkdir(parents=True, exist_ok=True)
                copy_atomic(sync_unit, profile_unit, report)
                synced_list.append(1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for fut in as_completed({pool.submit(_sync_unit, n): n for n in unit_names}):
            fut.result()

    return len(synced_list), len(skipped_list)
