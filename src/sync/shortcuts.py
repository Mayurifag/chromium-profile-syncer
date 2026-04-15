from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import sqlite3
import struct
import uuid
from collections.abc import Callable
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from src.sync import _noop

_LOG = logging.getLogger(__name__)


def load_oscrypt_key(user_data_dir: Path) -> AESGCM | None:
    if platform.system() != "Windows":
        return None
    try:
        import base64
        import ctypes
        import ctypes.wintypes

        local_state = json.loads(
            (user_data_dir / "Local State").read_text(encoding="utf-8")
        )
        enc_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])[5:]

        class _BLOB(ctypes.Structure):
            _fields_ = [
                ("cbData", ctypes.wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_char)),
            ]

        buf = ctypes.create_string_buffer(enc_key)
        blob_in = _BLOB(len(enc_key), buf)
        blob_out = _BLOB()
        ok = ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
        )
        if not ok:
            return None
        return AESGCM(ctypes.string_at(blob_out.pbData, blob_out.cbData))
    except Exception:
        return None


def make_url_hash(row_id: int, url: str, aesgcm: AESGCM) -> bytes:
    url_b = url.encode("utf-8")
    pad = (4 - len(url_b) % 4) % 4
    payload = struct.pack("<q", row_id) + struct.pack("<I", len(url_b)) + url_b + bytes(pad)
    pickle_bytes = struct.pack("<I", len(payload)) + payload
    plaintext = b"\x01" + hashlib.sha256(pickle_bytes).digest()
    nonce = os.urandom(12)
    return b"v10" + nonce + aesgcm.encrypt(nonce, plaintext, None)


def extract_search_shortcuts(
    profile_path: Path,
    sync_folder_root: Path,
    report: Callable[[str], None] = _noop,
) -> None:
    web_data_src = profile_path / "Web Data"
    shortcuts_json = sync_folder_root / "search_shortcuts.json"

    if not web_data_src.exists():
        _LOG.debug("No Web Data database found at %s — skipping extract", web_data_src)
        return

    try:
        prefs_path = profile_path / "Preferences"
        default_guid = ""
        default_engine_url = ""
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            default_guid = prefs.get("default_search_provider", {}).get("guid", "")
            dspd = prefs.get("default_search_provider_data", {}).get(
                "mirrored_template_url_data", {}
            )
            default_engine_url = dspd.get("url", "")

        conn = sqlite3.connect(f"file:{web_data_src}?mode=ro&immutable=1", uri=True)
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT keyword, short_name, url, favicon_url, suggest_url,
                       prepopulate_id, is_active, date_created, last_modified,
                       sync_guid, safe_for_autoreplace, input_encodings, alternate_urls
                FROM keywords
                WHERE prepopulate_id = 0
                ORDER BY keyword
                """
            )
            rows = cursor.fetchall()
        finally:
            conn.close()

        shortcuts = []
        for row in rows:
            sync_guid = row[9] or ""
            is_default = False
            if default_guid:
                if sync_guid == default_guid:
                    is_default = True
                elif not sync_guid and default_engine_url and row[2] == default_engine_url:
                    # DB sync_guid is empty but URL matches the default engine in Preferences;
                    # adopt the known guid so it survives round-trip through the JSON.
                    sync_guid = default_guid
                    is_default = True
            elif default_engine_url and row[2] == default_engine_url:
                is_default = True
            shortcuts.append(
                {
                    "keyword": row[0],
                    "short_name": row[1],
                    "url": row[2],
                    "favicon_url": row[3],
                    "suggest_url": row[4],
                    "prepopulate_id": row[5],
                    "is_active": row[6],
                    "date_created": row[7],
                    "last_modified": row[8],
                    "sync_guid": sync_guid,
                    "safe_for_autoreplace": row[10] if row[10] is not None else 0,
                    "input_encodings": row[11] or "UTF-8",
                    "alternate_urls": row[12] or "[]",
                    "is_default": is_default,
                }
            )

        shortcuts_json.write_text(json.dumps(shortcuts, indent=2), encoding="utf-8")
        report("search_shortcuts.json")
        _LOG.info("Extracted %d user search shortcuts to %s", len(shortcuts), shortcuts_json)

    except sqlite3.Error as exc:
        _LOG.warning("Failed to extract search shortcuts from %s: %s", web_data_src, exc)


def restore_search_shortcuts(
    profile_path: Path,
    sync_folder_root: Path,
    report: Callable[[str], None] = _noop,
) -> None:
    web_data_dst = profile_path / "Web Data"
    shortcuts_json = sync_folder_root / "search_shortcuts.json"

    if not shortcuts_json.exists():
        _LOG.debug("No search_shortcuts.json found at %s", shortcuts_json)
        return

    if not web_data_dst.exists():
        _LOG.warning("Web Data database not found at %s — cannot restore", web_data_dst)
        return

    try:
        shortcuts = json.loads(shortcuts_json.read_text(encoding="utf-8"))

        prefs_path = profile_path / "Preferences"
        prefs: dict | None = None
        if prefs_path.exists():
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))

        # On Windows, Chromium verifies url_hash on startup and drops rows
        # whose hash doesn't match Pickle(id, url). Load the OSCrypt key
        # once so we can compute a valid blob for every inserted row.
        aesgcm = load_oscrypt_key(profile_path.parent)
        if platform.system() == "Windows" and aesgcm is None:
            _LOG.warning(
                "Cannot restore search shortcuts to %s: OSCrypt key unavailable "
                "(launch the browser once to initialize Local State, then apply backup again)",
                profile_path.name,
            )
            return

        conn = sqlite3.connect(str(web_data_dst))
        cursor = conn.cursor()

        cursor.execute("DELETE FROM keywords WHERE prepopulate_id = 0")

        next_id = (
            cursor.execute("SELECT COALESCE(MAX(id), 0) FROM keywords").fetchone()[0] + 1
        )

        restored_default_guid: str | None = None
        default_shortcut: dict | None = None
        default_row_id: int | None = None

        for i, shortcut in enumerate(shortcuts):
            row_id = next_id + i
            sync_guid = shortcut.get("sync_guid") or ""
            if shortcut.get("is_default"):
                if not sync_guid:
                    sync_guid = str(uuid.uuid4())
                restored_default_guid = sync_guid
                default_shortcut = shortcut
                default_row_id = row_id

            url_hash_blob: bytes | None = None
            if aesgcm is not None:
                url_hash_blob = make_url_hash(row_id, shortcut["url"], aesgcm)

            cursor.execute(
                """
                INSERT INTO keywords (
                    id, short_name, keyword, favicon_url, url, safe_for_autoreplace,
                    originating_url, date_created, usage_count, input_encodings,
                    suggest_url, prepopulate_id, created_by_policy, last_modified,
                    sync_guid, alternate_urls, image_url, search_url_post_params,
                    suggest_url_post_params, image_url_post_params, new_tab_url,
                    last_visited, created_from_play_api, is_active, starter_pack_id,
                    enforced_by_policy, featured_by_policy, url_hash
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    row_id,
                    shortcut["short_name"],
                    shortcut["keyword"],
                    shortcut.get("favicon_url", ""),
                    shortcut["url"],
                    shortcut.get("safe_for_autoreplace", 0),
                    "",
                    shortcut.get("date_created", 0),
                    0,
                    shortcut.get("input_encodings", "UTF-8"),
                    shortcut.get("suggest_url", ""),
                    shortcut.get("prepopulate_id", 0),
                    0,
                    shortcut.get("last_modified", 0),
                    sync_guid,
                    shortcut.get("alternate_urls", "[]"),
                    "",
                    "",
                    "",
                    "",
                    "",
                    shortcut.get("last_modified", 0),
                    0,
                    shortcut.get("is_active", 1),
                    0,
                    0,
                    0,
                    url_hash_blob,
                ),
            )

        # Sync the default-engine pointer in keywords_metadata so Chromium finds
        # our inserted engine when it validates the default on startup — otherwise
        # the stale ID causes Chromium to fall into its built-in repopulation path
        # and reset the default to DuckDuckGo / its own choice.
        # Also delete the encrypted backup blob: it anchors the old default and
        # Chromium will use it to overwrite our Preferences guid if it exists.
        if default_row_id is not None:
            try:
                updated = cursor.execute(
                    "UPDATE keywords_metadata SET value = ? "
                    "WHERE key = 'Default Search Provider ID'",
                    (str(default_row_id),),
                ).rowcount
                if updated == 0:
                    cursor.execute(
                        "INSERT OR IGNORE INTO keywords_metadata (key, value) VALUES (?, ?)",
                        ("Default Search Provider ID", str(default_row_id)),
                    )
                cursor.execute(
                    "DELETE FROM keywords_metadata "
                    "WHERE key = 'Default Search Provider Backup'",
                )
            except sqlite3.OperationalError:
                _LOG.debug("keywords_metadata unavailable — skipping ID sync")

        conn.commit()
        conn.close()

        if restored_default_guid and prefs is not None:
            dsp = prefs.setdefault("default_search_provider", {})
            dsp["guid"] = restored_default_guid
            dsp.pop("reset_occurred", None)
            dsp.pop("reset_time", None)

            if default_shortcut is not None and default_row_id is not None:
                alt_urls_raw = default_shortcut.get("alternate_urls", "[]")
                try:
                    alt_urls = (
                        json.loads(alt_urls_raw)
                        if isinstance(alt_urls_raw, str)
                        else alt_urls_raw
                    )
                except (json.JSONDecodeError, TypeError):
                    alt_urls = []
                mirror = {
                    "alternate_urls": alt_urls,
                    "contextual_search_url": "",
                    "created_from_play_api": False,
                    "date_created": str(default_shortcut.get("date_created", 0)),
                    "favicon_url": default_shortcut.get("favicon_url", ""),
                    "id": str(default_row_id),
                    "image_search_branding_label": "",
                    "image_search_post_params": "",
                    "image_translate_source_language_param_key": "",
                    "image_translate_source_language_param_value": "",
                    "image_translate_target_language_param_key": "",
                    "image_url": "",
                    "image_url_post_params": "",
                    "is_active": default_shortcut.get("is_active", 1),
                    "keyword": default_shortcut.get("keyword", ""),
                    "last_modified": str(default_shortcut.get("last_modified", 0)),
                    "logo_url": "",
                    "new_tab_url": "",
                    "policy_origin": "",
                    "prepopulate_id": default_shortcut.get("prepopulate_id", 0),
                    "safe_for_autoreplace": bool(
                        default_shortcut.get("safe_for_autoreplace", False)
                    ),
                    "search_intent_params": [],
                    "short_name": default_shortcut.get("short_name", ""),
                    "side_image_search_param": "",
                    "suggestions_url": default_shortcut.get("suggest_url", ""),
                    "synced_guid": restored_default_guid,
                    "url": default_shortcut.get("url", ""),
                    "visual_url": "",
                }
                prefs.setdefault("default_search_provider_data", {})[
                    "mirrored_template_url_data"
                ] = mirror

            prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

        report("search shortcuts restored")
        _LOG.info("Restored %d search shortcuts from %s", len(shortcuts), shortcuts_json)

    except (sqlite3.Error, json.JSONDecodeError, KeyError) as exc:
        _LOG.warning("Failed to restore search shortcuts: %s", exc)
