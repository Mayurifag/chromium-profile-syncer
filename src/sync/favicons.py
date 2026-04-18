from __future__ import annotations

import json
import logging
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlparse

from src.sync import _noop

_LOG = logging.getLogger(__name__)


def _collect_page_urls(profile_path: Path, sync_profile_path: Path) -> set[str]:
    urls: set[str] = set()

    bm_file = profile_path / "Bookmarks"
    if bm_file.exists():
        try:
            _walk_bookmarks(json.loads(bm_file.read_bytes()).get("roots", {}), urls)
        except (json.JSONDecodeError, OSError):
            pass

    ss_file = sync_profile_path / "search_shortcuts.json"
    if ss_file.exists():
        try:
            for sc in json.loads(ss_file.read_bytes()):
                fav = sc.get("favicon_url", "")
                if fav:
                    urls.add(fav)
        except (json.JSONDecodeError, OSError):
            pass

    origins = {
        f"{p.scheme}://{p.netloc}/"
        for u in set(urls)
        if (p := urlparse(u)).scheme in ("http", "https") and p.netloc
    }
    return urls | origins


def _walk_bookmarks(node: object, urls: set[str]) -> None:
    if isinstance(node, dict):
        if node.get("type") == "url" and "url" in node:
            urls.add(node["url"])
        for child in node.get("children", []):
            _walk_bookmarks(child, urls)
    elif isinstance(node, list):
        for item in node:
            _walk_bookmarks(item, urls)


def extract_favicons(
    profile_path: Path,
    sync_profile_path: Path,
    report: Callable[[str], None] = _noop,
) -> None:
    src = profile_path / "Favicons"
    if not src.exists():
        return

    urls = _collect_page_urls(profile_path, sync_profile_path)
    if not urls:
        return

    tmp_path: Path | None = None
    src_conn: sqlite3.Connection | None = None
    dst_conn: sqlite3.Connection | None = None
    try:
        src_conn = sqlite3.connect(f"file:{src}?mode=ro&immutable=1", uri=True)

        ph = ",".join("?" * len(urls))
        cur = src_conn.execute(
            f"SELECT * FROM icon_mapping WHERE page_url IN ({ph})",
            list(urls),
        )
        col_names = [d[0] for d in cur.description]
        icon_id_idx = col_names.index("icon_id")
        mappings = cur.fetchall()

        if not mappings:
            _LOG.debug("No favicon mappings found for collected URLs")
            return

        icon_ids = list({row[icon_id_idx] for row in mappings})
        id_ph = ",".join("?" * len(icon_ids))

        favicon_rows = src_conn.execute(
            f"SELECT * FROM favicons WHERE id IN ({id_ph})", icon_ids
        ).fetchall()
        bitmap_rows = src_conn.execute(
            f"SELECT * FROM favicon_bitmaps WHERE icon_id IN ({id_ph})", icon_ids
        ).fetchall()
        meta_rows = src_conn.execute("SELECT * FROM meta").fetchall()
        schema_rows = src_conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND sql IS NOT NULL"
        ).fetchall()

        with tempfile.NamedTemporaryFile(suffix=".db.tmp", delete=False) as ntf:
            tmp_path = Path(ntf.name)

        dst_conn = sqlite3.connect(str(tmp_path))
        with dst_conn:
            for (sql,) in schema_rows:
                dst_conn.execute(sql)
            dst_conn.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", meta_rows)
            dst_conn.executemany(
                f"INSERT INTO icon_mapping VALUES ({','.join('?' * len(mappings[0]))})",
                mappings,
            )
            if favicon_rows:
                dst_conn.executemany(
                    f"INSERT INTO favicons VALUES ({','.join('?' * len(favicon_rows[0]))})",
                    favicon_rows,
                )
            if bitmap_rows:
                dst_conn.executemany(
                    f"INSERT INTO favicon_bitmaps VALUES ({','.join('?' * len(bitmap_rows[0]))})",
                    bitmap_rows,
                )

        dst_conn.close()
        dst_conn = None
        src_conn.close()
        src_conn = None

        sync_profile_path.mkdir(parents=True, exist_ok=True)
        shutil.move(str(tmp_path), sync_profile_path / "Favicons")
        tmp_path = None
        report("Favicons")
        _LOG.info("Extracted %d favicon(s) for %d page URL(s)", len(favicon_rows), len(mappings))

    except sqlite3.Error:
        _LOG.exception("Failed to extract favicons from %s", src)
    finally:
        if src_conn:
            src_conn.close()
        if dst_conn:
            dst_conn.close()
        if tmp_path:
            tmp_path.unlink(missing_ok=True)


def restore_favicons(
    profile_path: Path,
    sync_profile_path: Path,
    report: Callable[[str], None] = _noop,
) -> None:
    src = sync_profile_path / "Favicons"
    if not src.exists():
        return
    dst = profile_path / "Favicons"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    report("Favicons")
    _LOG.info("Restored Favicons to %s", dst)
