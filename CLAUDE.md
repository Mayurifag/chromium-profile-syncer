# Project Conventions

## Code

- Never hand-edit `uv.lock`. Use `uv add` / `uv remove` for deps.
- No backward compat — project is author-only. Less code, brave refactors, keep clean.

## Running Commands

Always prefix with `uv run`:

~~~sh
uv run pytest
uv run ruff check src tests
uv run python -m src.main
uv run python build.py
~~~

## After Fixes

Reinstall and relaunch after code changes:

~~~sh
make install
~~~

Builds app, kills running instance, installs, launches.

## Technology Constraints

- **PySide6** — no PyQt6 or other bindings.
- **Python 3.12+** — no syntax/stdlib unavailable in 3.12.
- **rclone** — required runtime dep for sync progress (install via system package manager).

## Project Structure

- `src/` — app source package
- `tests/` — test files
- `build.py` — PyInstaller build script
- `pyproject.toml` — project metadata and tool config

## Code Style

- Line length: 100 characters
- Ruff rules: E, F, I, UP
- Target: Python 3.12
- **No obvious comments** — only explain *why*, not *what*. Code self-documents via naming.

## Key Implementation Details

### Windows Registry — Extension Policy Keys

**Never wipe `HKCU\SOFTWARE\Policies\*` or `HKCU\SOFTWARE\Chromium\Extensions`
(or any shared Chromium registry key) outside of what `_install_extensions_via_registry`
and `_install_extensions_via_force_list` already manage.**

Keys shared across all Chromium-based browsers. Bulk-deleting (e.g. browser cleanup) silently breaks extension install for unrelated browsers reading same paths.

### Smart Extension Syncing

**Detection:** Web Store extensions have `_metadata/verified_contents.json` (Google signature)
- **Web Store extensions:** Save only extension ID to `webstore_extensions.json` manifest
- **Unpacked extensions:** Sync full code (can't re-download)
- **Auto-installation:** Generate External Extensions JSON stubs from manifest

### Trash File Exclusion

Files excluded from sync:
- `._*` — macOS metadata on exFAT/FAT32 drives

**Implementation:**
- rclone: `--exclude "._*"`
- shutil.copytree: `ignore=shutil.ignore_patterns("._*")`

### Tar Archive Sync

All profile data packed into single `current.tar` before sync:
- Written to temp file outside sync folder, moved atomically
- Unpacked to system temp during restore; cloud client sees only `current.tar` change
- No backup rotation folders (`backup-1/`, `backup-2/`)

### Search Shortcuts Sync

Extracts user-created search engines (`prepopulate_id = 0`) from `Web Data` SQLite, stores as `search_shortcuts.json` inside tar (at `work_dir` root).

**Windows-specific — url_hash mandatory:**
- Every keywords row needs valid 64-byte `url_hash` BLOB
- Formula: `b'v10' + nonce(12) + AES-256-GCM(key, nonce, b'\x01' + SHA256(Pickle(id, url))) + tag(16)`
- AES key from `Local State` → `os_crypt.encrypted_key` (strip 5-byte `DPAPI` prefix, DPAPI-decrypt)
- Rows with missing/invalid hash silently dropped at Chromium startup
- See `docs/search-shortcuts.md` for full formula and Python implementation

**sync_guid rules:**
- Default engine: `sync_guid` must match `Preferences["default_search_provider"]["guid"]`
- All others: `sync_guid = ""` (local-only; Chrome deletes unknown UUIDs on reconciliation)

**Restore scope:** `DELETE FROM keywords WHERE prepopulate_id = 0` — never touch built-ins.

### First-Sync Detection

- Check if `metadata.json` exists before sync
- Missing → first-time setup (shows "Initial setup complete")
- Exists → regular sync (shows "Last sync: [timestamp]")
- Prevents "sync complete" spam on clean slate

### Clean Button (Settings Window)

- Visible only when sync folder has data (`current.tar` exists or `current/` dir exists)
- Deletes all synced data: `current.tar`, `current/` (legacy), `metadata.json` (`search_shortcuts.json` inside tar, deleted with it)
- Clears enabled profiles and browsers from config
- Shows confirmation dialog before deletion
- After clean, triggers initial upload dialog to start fresh