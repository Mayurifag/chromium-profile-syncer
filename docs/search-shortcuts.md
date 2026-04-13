# Search Shortcuts — Implementation Notes

Everything learned while debugging cross-machine search engine restore on Windows/Thorium (April 2026).

---

## How Chromium stores custom search engines

Custom search engines (user-created, not Google/Bing/etc.) live in:

~~~
User Data/Profile N/Web Data   (SQLite)
  table: keywords
~~~

Relevant columns:

| Column | Notes |
|--------|-------|
| `id` | INTEGER PRIMARY KEY, auto-increment |
| `keyword` | The shortcut typed in the omnibox (e.g. `gru`) |
| `short_name` | Display name |
| `url` | Template URL, `{searchTerms}` placeholder |
| `prepopulate_id` | 0 for user-created; >0 for built-ins (Google=1, etc.) |
| `sync_guid` | UUID string; empty = local-only engine |
| `is_active` | 1 = active |
| `url_hash` | 64-byte BLOB — **mandatory on Windows** (see below) |

The default search engine is identified by `Preferences["default_search_provider"]["guid"]`, which must match the `sync_guid` of the corresponding row.

---

## The url_hash — why it exists and what it contains

Chromium added `url_hash` as a **tamper-detection mechanism** for the keywords table.

**On Windows only** (`#if BUILDFLAG(IS_WIN)`), the `TemplateURLService` verifies this blob for every row on startup. Any row with a missing (`NULL`) or invalid `url_hash` is **silently dropped** from the loaded engine set — it just disappears as if it was never there. macOS and Linux skip this check entirely.

### Formula

~~~
plaintext  = b'\x01' + SHA-256( Pickle(WriteInt64(id), WriteString(url)) )
url_hash   = b'v10' + nonce(12 bytes) + AES-256-GCM(key, nonce, plaintext) + tag(16 bytes)
~~~

Total blob size: `3 + 12 + 33 + 16 = 64 bytes` (always).

### Pickle format

`base::Pickle` layout used by Chromium's `WriteInt64` / `WriteString`:

~~~
[ 4 bytes LE ]  payload_size  (total bytes that follow)
[ 8 bytes LE ]  int64 value   (the row id)
[ 4 bytes LE ]  string length (byte count of UTF-8 url)
[ N bytes    ]  UTF-8 url bytes
[ 0–3 bytes  ]  zero-padding to next 4-byte boundary
~~~

### AES key

The AES-256 key is stored DPAPI-encrypted in:

~~~
User Data/Local State
  os_crypt.encrypted_key   (base64-encoded; strip 5-byte "DPAPI" prefix before decrypting)
~~~

Decrypt with `CryptUnprotectData` (Windows DPAPI), feed the 32-byte result into `AESGCM`.

### Python implementation

~~~python
import hashlib, os, struct, json, base64, ctypes, ctypes.wintypes
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

def load_oscrypt_key(user_data_dir: Path) -> AESGCM | None:
    try:
        local_state = json.loads((user_data_dir / "Local State").read_text("utf-8"))
        enc = base64.b64decode(local_state["os_crypt"]["encrypted_key"])[5:]

        class _B(ctypes.Structure):
            _fields_ = [("cbData", ctypes.wintypes.DWORD),
                        ("pbData", ctypes.POINTER(ctypes.c_char))]

        buf = ctypes.create_string_buffer(enc)
        bi = _B(len(enc), buf); bo = _B()
        ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo))
        return AESGCM(ctypes.string_at(bo.pbData, bo.cbData))
    except Exception:
        return None

def make_url_hash(row_id: int, url: str, aesgcm: AESGCM) -> bytes:
    url_b = url.encode("utf-8")
    pad   = (4 - len(url_b) % 4) % 4
    payload = struct.pack("<q", row_id) + struct.pack("<I", len(url_b)) + url_b + bytes(pad)
    pickle  = struct.pack("<I", len(payload)) + payload
    pt      = b"\x01" + hashlib.sha256(pickle).digest()   # 33 bytes
    nonce   = os.urandom(12)
    return b"v10" + nonce + aesgcm.encrypt(nonce, pt, None)  # 64 bytes
~~~

**Critical**: the `row_id` used here must equal the actual `id` value in the DB row. If they differ, Chromium's verification fails and the row is dropped. Always insert with an **explicit id** (not auto-increment) so the id is known before you call `make_url_hash`.

---

## sync_guid — what it controls

| sync_guid value | Behaviour |
|-----------------|-----------|
| Empty string `""` | Local-only engine. Chrome's sync reconciliation ignores it. Safe for manually-inserted engines. |
| Non-empty UUID | Treated as a synced entity. If the UUID doesn't exist on the Google sync server, Chrome deletes the row during sync reconciliation — **even when server sync is fully disabled**. |

**Rule**: only the default search engine should carry a non-empty `sync_guid` (so it matches `Preferences["default_search_provider"]["guid"]`). All other user-created engines should have `sync_guid = ""`.

---

## Why external DB inserts get wiped — the full failure chain

We hit every failure mode in sequence before finding the root cause:

1. **First attempt** (Thorium was still running): inserted rows with random UUIDs. Thorium flushed its own in-memory state on close and overwrote the DB. All rows gone.

2. **Second attempt** (Thorium stopped, random UUIDs, wrong Preferences guid): rows had `url_hash = NULL`. Thorium started → tamper check → all rows silently dropped.

3. **Third attempt** (empty sync_guid, no Preferences change): still `url_hash = NULL`. Same result. Additionally, `Preferences["default_search_provider"]["guid"]` pointed to a guid that no row possessed → Chromium entered recovery mode and wiped all `prepopulate_id = 0` rows.

4. **Fourth attempt** (correct guid for default engine, still `url_hash = NULL`): same wipe.

5. **Root cause found**: `url_hash` must be computed and inserted with the row. Once we provided a valid 64-byte blob for every row, all engines survived startup.

---

## Restoring search engines manually (one-off script)

~~~python
import hashlib, json, os, sqlite3, struct, base64, ctypes, ctypes.wintypes
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

SHORTCUTS_JSON = Path("D:/OpenCloud/.../search_shortcuts.json")
WEB_DATA       = Path("C:/Users/.../Thorium/User Data/Profile 1/Web Data")
PREFS          = Path("C:/Users/.../Thorium/User Data/Profile 1/Preferences")
USER_DATA      = WEB_DATA.parent.parent  # "User Data" dir

# -- load key --
local_state = json.loads((USER_DATA / "Local State").read_text("utf-8"))
enc = base64.b64decode(local_state["os_crypt"]["encrypted_key"])[5:]
class _B(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
buf = ctypes.create_string_buffer(enc); bi = _B(len(enc), buf); bo = _B()
ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(bi), None, None, None, None, 0, ctypes.byref(bo))
aesgcm = AESGCM(ctypes.string_at(bo.pbData, bo.cbData))

def url_hash(row_id, url):
    url_b = url.encode(); pad = (4 - len(url_b) % 4) % 4
    payload = struct.pack("<q", row_id) + struct.pack("<I", len(url_b)) + url_b + bytes(pad)
    pt = b"\x01" + hashlib.sha256(struct.pack("<I", len(payload)) + payload).digest()
    n = os.urandom(12)
    return b"v10" + n + aesgcm.encrypt(n, pt, None)

# -- restore --
shortcuts = json.loads(SHORTCUTS_JSON.read_text("utf-8"))
DEFAULT_GUID = "ba9052d9-0434-40cc-abb9-9d5392570a9b"   # engine's sync_guid = Preferences guid

conn = sqlite3.connect(str(WEB_DATA))
conn.execute("DELETE FROM keywords WHERE prepopulate_id = 0")
next_id = conn.execute("SELECT COALESCE(MAX(id),0) FROM keywords").fetchone()[0] + 1

for i, s in enumerate(shortcuts):
    rid = next_id + i
    sg  = DEFAULT_GUID if s.get("is_default") else ""
    conn.execute("""
        INSERT INTO keywords (id, short_name, keyword, favicon_url, url,
            safe_for_autoreplace, originating_url, date_created, usage_count,
            input_encodings, suggest_url, prepopulate_id, created_by_policy,
            last_modified, sync_guid, alternate_urls, image_url,
            search_url_post_params, suggest_url_post_params, image_url_post_params,
            new_tab_url, last_visited, created_from_play_api, is_active,
            starter_pack_id, enforced_by_policy, featured_by_policy, url_hash)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, (rid, s["short_name"], s["keyword"], s.get("favicon_url",""), s["url"],
          s.get("safe_for_autoreplace",0), "", s.get("date_created",0), 0,
          s.get("input_encodings","UTF-8"), s.get("suggest_url",""), 0, 0,
          s.get("last_modified",0), sg, s.get("alternate_urls","[]"),
          "", "", "", "", "", s.get("last_modified",0), 0,
          s.get("is_active",1), 0, 0, 0, url_hash(rid, s["url"])))

conn.commit(); conn.close()

# Update Preferences so the default engine is recognised
prefs = json.loads(PREFS.read_text("utf-8"))
prefs.setdefault("default_search_provider", {})["guid"] = DEFAULT_GUID
PREFS.write_text(json.dumps(prefs), "utf-8")
print("Done")
~~~

---

## search_shortcuts.json format

Stored at the **sync folder root** (shared across all browsers syncing to the same folder).

~~~json
[
  {
    "keyword":            "gru",
    "short_name":         "Google Russia",
    "url":                "https://www.google.com/search?q={searchTerms}&gl=ru&hl=ru&pws=0",
    "favicon_url":        "https://...",
    "suggest_url":        "",
    "prepopulate_id":     0,
    "is_active":          1,
    "date_created":       13417245484683053,
    "last_modified":      13417245590570218,
    "sync_guid":          "ba9052d9-0434-40cc-abb9-9d5392570a9b",
    "safe_for_autoreplace": 0,
    "input_encodings":    "UTF-8",
    "alternate_urls":     "[]",
    "is_default":         true
  }
]
~~~

Only one entry should have `"is_default": true`. That engine's `sync_guid` must match `Preferences["default_search_provider"]["guid"]`.

---

## Extraction rules

Only `prepopulate_id = 0` rows are extracted (user-created only). Built-in engines (Google, Bing, etc.) are never backed up — they are reinstalled by the browser itself.

When the default engine has an **empty sync_guid in the DB** (can happen if Chromium wrote the guid only to Preferences but not yet to the DB), the extraction code identifies it by matching `url` against `Preferences["default_search_provider_data"]["mirrored_template_url_data"]["url"]` and adopts the guid from `Preferences["default_search_provider"]["guid"]`.

---

## Restore rules

1. `DELETE FROM keywords WHERE prepopulate_id = 0` (never touch built-ins).
2. Get `next_id = MAX(id) + 1` after the delete.
3. For each engine: assign explicit `id = next_id + i`, compute `url_hash`, insert.
4. Non-default engines: `sync_guid = ""` (local-only).
5. Default engine: `sync_guid` = its known guid from the JSON.
6. After inserting: write `sync_guid` of the default engine into `Preferences["default_search_provider"]["guid"]`.

**Never** assign random UUIDs to non-default engines — Chrome treats any unknown UUID as a synced entity and may delete it during reconciliation even with server sync disabled.
