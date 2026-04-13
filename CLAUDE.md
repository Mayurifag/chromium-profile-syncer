# Project Conventions

## Dependency Management

Never hand-edit `uv.lock`. Use `uv add` / `uv remove` to manage dependencies.

## Running Commands

Always prefix invocations with `uv run`:

~~~sh
uv run pytest
uv run ruff check src tests
uv run python -m src.main
uv run python build.py
~~~

## After Fixes

Always reinstall and relaunch after code changes:

~~~sh
make install
~~~

This builds the app, kills the running instance, installs, and launches automatically.

## Technology Constraints

- **PySide6** — do not substitute PyQt6 or any other binding.
- **Python 3.12+** — do not use syntax or stdlib features unavailable in 3.12.
- **rclone** — required runtime dependency for progress reporting during sync (must be installed via system package manager).

## Project Structure

- `src/` — application source package
- `tests/` — test files
- `build.py` — PyInstaller build script
- `pyproject.toml` — project metadata and tool configuration

## Code Style

- Line length: 100 characters
- Ruff rules: E, F, I, UP
- Target: Python 3.12
- **No obvious comments** — do not add comments that merely restate what the code does. Comments should only explain *why*, not *what*. The code itself should be self-explanatory through clear naming and structure.

## Key Implementation Details

### Smart Extension Syncing

**Detection:** Web Store extensions have `_metadata/verified_contents.json` (Google signature)
- **Web Store extensions:** Only save extension ID to `webstore_extensions.json` manifest
- **Unpacked extensions:** Sync full code (can't be re-downloaded)
- **Auto-installation:** Generate External Extensions JSON stubs from manifest

### Trash File Exclusion

Files excluded from sync:
- `._*` — macOS metadata on exFAT/FAT32 drives

**Implementation:**
- rclone: `--exclude "._*"`
- shutil.copytree: `ignore=shutil.ignore_patterns("._*")`

### Tar Archive Sync

All profile data is packed into a single `current.tar` before syncing:
- Written to a temp file outside the sync folder, then moved atomically
- Unpacked to a system temp directory during restore; cloud client only sees `current.tar` change
- No backup rotation folders (`backup-1/`, `backup-2/`)

### Search Shortcuts Sync

Extracts user-created search engines (`prepopulate_id = 0`) from `Web Data` SQLite and stores as `search_shortcuts.json` at the sync folder root.

**Windows-specific — url_hash is mandatory:**
- Every inserted keywords row must have a valid 64-byte `url_hash` BLOB
- Formula: `b'v10' + nonce(12) + AES-256-GCM(key, nonce, b'\x01' + SHA256(Pickle(id, url))) + tag(16)`
- AES key from `Local State` → `os_crypt.encrypted_key` (strip 5-byte `DPAPI` prefix, then DPAPI-decrypt)
- Rows with missing/invalid hash are silently dropped at Chromium startup
- See `docs/search-shortcuts.md` for the full formula and Python implementation

**sync_guid rules:**
- Default engine: `sync_guid` must match `Preferences["default_search_provider"]["guid"]`
- All other engines: `sync_guid = ""` (local-only; Chrome deletes unknown UUIDs on reconciliation)

**Restore scope:** `DELETE FROM keywords WHERE prepopulate_id = 0` — never touch built-ins.

### First-Sync Detection

- Checks if `metadata.json` exists before syncing
- If missing → first-time setup (shows "Initial setup complete")
- If exists → regular sync (shows "Last sync: [timestamp]")
- Prevents "sync complete" spam on clean slate

### Clean Button (Settings Window)

- Visible only when sync folder contains data (`current.tar` exists or `current/` directory exists)
- Deletes all synced data: `current.tar`, `current/` (legacy), `metadata.json`, `search_shortcuts.json`
- Clears enabled profiles and browsers from config
- Shows confirmation dialog before deletion
- After cleaning, triggers initial upload dialog to start fresh
