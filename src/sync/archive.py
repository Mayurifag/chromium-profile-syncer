from __future__ import annotations

import logging
import shutil
import tarfile
import tempfile
import time
from pathlib import Path

_LOG = logging.getLogger(__name__)


def validate_archive_content(work_dir: Path) -> bool:
    def _dir_nonempty(p: Path) -> bool:
        return p.is_dir() and any(p.iterdir())

    checks: dict[str, bool] = {
        "extensions (unpacked)": _dir_nonempty(work_dir / "Extensions"),
        "extensions (webstore manifest)": (work_dir / "webstore_extensions.json").is_file(),
        "local_extension_settings": _dir_nonempty(work_dir / "Local Extension Settings"),
        "indexed_db (extensions)": (work_dir / "IndexedDB").is_dir() and any(
            d for d in (work_dir / "IndexedDB").iterdir()
            if d.is_dir() and d.name.startswith("chrome-extension_")
        ),
        "local_storage": _dir_nonempty(work_dir / "Local Storage" / "leveldb"),
        "bookmarks": (work_dir / "Bookmarks").is_file(),
        "custom_dictionary": (work_dir / "Custom Dictionary.txt").is_file(),
        "preferences": (work_dir / "preferences.json").is_file(),
        "search_shortcuts": (work_dir / "search_shortcuts.json").is_file(),
        "favicons": (work_dir / "Favicons").is_file(),
        "omnibox_shortcuts": (work_dir / "Shortcuts").is_file(),
    }

    for item, present in checks.items():
        if present:
            _LOG.debug("Archive check OK: %s", item)
        else:
            _LOG.warning("Archive check MISSING: %s", item)

    present_items = [k for k, v in checks.items() if v]
    missing_items = [k for k, v in checks.items() if not v]

    if missing_items:
        _LOG.warning(
            "Archive integrity: %d/%d items present, missing: %s",
            len(present_items),
            len(checks),
            ", ".join(missing_items),
        )

    if not present_items:
        _LOG.error(
            "Archive integrity check failed: no expected items found in staging dir "
            "— skipping pack to avoid overwriting cloud archive with empty data"
        )
        return False

    return True


def pack_to_archive(src_dir: Path, dst_archive: Path) -> None:
    with tempfile.NamedTemporaryFile(suffix=".tar.tmp", delete=False) as ntf:
        tmp = Path(ntf.name)
    try:
        with tarfile.open(str(tmp), "w:") as tf:
            tf.add(str(src_dir), arcname=".")
        for attempt in range(6):
            try:
                shutil.copy2(str(tmp), dst_archive)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.3)
    finally:
        tmp.unlink(missing_ok=True)


def unpack_archive(src_archive: Path, dst_dir: Path) -> None:
    dst_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(str(src_archive)) as tf:
        tf.extractall(str(dst_dir), filter="data")
