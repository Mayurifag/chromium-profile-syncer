from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable
from pathlib import Path

from src.sync import _noop, write_text_if_changed

_LOG = logging.getLogger(__name__)


def snapshot_typed_urls(profile_path: Path) -> list[dict] | None:
    history_db = profile_path / "History"
    if not history_db.exists():
        return None

    try:
        conn = sqlite3.connect(f"file:{history_db}?mode=ro&immutable=1", uri=True)
        try:
            rows = conn.execute(
                "SELECT url, title, typed_count, last_visit_time FROM urls "
                "WHERE typed_count > 0 ORDER BY url"
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _LOG.warning("Failed to snapshot typed URLs from %s: %s", history_db, exc)
        return None

    return [
        {"url": r[0], "title": r[1], "typed_count": r[2], "last_visit_time": r[3]}
        for r in rows
    ]


def extract_typed_urls(
    profile_path: Path,
    sync_dir: Path,
    report: Callable[[str], None] = _noop,
) -> None:
    data = snapshot_typed_urls(profile_path)
    if data is None:
        return

    out = sync_dir / "typed_urls.json"
    if not write_text_if_changed(out, json.dumps(data)):
        _LOG.debug("typed_urls.json unchanged — skipping write")
        return
    report("typed_urls.json")
    _LOG.info("Extracted %d typed URLs to %s", len(data), out)


def restore_typed_urls(
    profile_path: Path,
    sync_dir: Path,
    report: Callable[[str], None] = _noop,
) -> None:
    src = sync_dir / "typed_urls.json"
    if not src.exists():
        return

    history_db = profile_path / "History"
    if not history_db.exists():
        _LOG.warning("History db not found at %s — cannot restore typed URLs", history_db)
        return

    try:
        data: list[dict] = json.loads(src.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        _LOG.warning("Failed to read typed_urls.json: %s", exc)
        return

    try:
        conn = sqlite3.connect(str(history_db))
        try:
            for entry in data:
                url = entry["url"]
                title = entry.get("title", "")
                typed_count = int(entry.get("typed_count", 1))
                last_visit_time = int(entry.get("last_visit_time", 0))

                row = conn.execute(
                    "SELECT id, typed_count, last_visit_time FROM urls WHERE url = ?", (url,)
                ).fetchone()

                if row:
                    conn.execute(
                        "UPDATE urls SET typed_count = ?, last_visit_time = ? WHERE id = ?",
                        (row[1] + typed_count, max(row[2], last_visit_time), row[0]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO urls"
                        " (url, title, visit_count, typed_count, last_visit_time, hidden)"
                        " VALUES (?, ?, ?, ?, ?, 0)",
                        (url, title, typed_count, typed_count, last_visit_time),
                    )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        _LOG.warning("Failed to restore typed URLs into %s: %s", history_db, exc)
        return

    report("typed_urls.json")
    _LOG.info("Restored/merged %d typed URLs from %s", len(data), src)
