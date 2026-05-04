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


def is_empty_leveldb(d: Path) -> bool:
    if not d.is_dir():
        return False
    try:
        for ldb in d.glob("*.ldb"):
            if ldb.is_file() and ldb.stat().st_size > 0:
                return False
        for log in d.glob("*.log"):
            if log.is_file() and log.stat().st_size > 0:
                return False
    except OSError:
        return False
    return True


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
        _LOG.exception("Atomic copy failed: %s -> %s (dst untouched)", src, dst)


def sync_dir(
    profile_dir: Path,
    sync_path: Path,
    subpath: str,
    direction: str = "both",
    report: Callable[[str], None] = _noop,
    name_filter: Callable[[str], bool] | None = None,
    key_skip_prefixes: dict[str, tuple[bytes, ...]] | None = None,
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

        profile_newer = profile_mtime > sync_mtime
        # An empty leveldb stub (browser-init only) must never overwrite the
        # other side's real data — mtime can favour the stub if it was touched
        # more recently (e.g. a fresh browser launch).
        profile_empty = is_empty_leveldb(profile_unit) if profile_unit.exists() else True
        sync_empty = is_empty_leveldb(sync_unit) if sync_unit.exists() else True

        if profile_newer and direction in ("push", "both"):
            if profile_empty and not sync_empty:
                _LOG.info(
                    "Skip push %s/%s: profile is empty stub, sync has data",
                    subpath, name,
                )
                skipped_list.append(1)
                return
            sync_base.mkdir(parents=True, exist_ok=True)
            prefixes = (key_skip_prefixes or {}).get(name)
            if prefixes and profile_unit.exists():
                from src.sync.ldb_filter import copy_filtered
                copy_filtered(profile_unit, sync_unit, prefixes)
            else:
                copy_atomic(profile_unit, sync_unit, report)
            synced_list.append(1)
        elif not profile_newer and direction in ("pull", "both"):
            if sync_empty and not profile_empty:
                _LOG.info(
                    "Skip pull %s/%s: sync is empty stub, profile has data",
                    subpath, name,
                )
                skipped_list.append(1)
                return
            profile_base.mkdir(parents=True, exist_ok=True)
            copy_atomic(sync_unit, profile_unit, report)
            synced_list.append(1)
        else:
            skipped_list.append(1)

    with ThreadPoolExecutor(max_workers=8) as pool:
        for fut in as_completed({pool.submit(_sync_unit, n): n for n in unit_names}):
            fut.result()

    return len(synced_list), len(skipped_list)
