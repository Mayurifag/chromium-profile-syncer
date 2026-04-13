# Chromium Profile Syncer — Design Document

## Overview

Cross-platform PySide6 system tray application that syncs Chromium browser profiles
(extensions, settings, bookmarks, search shortcuts) across machines via a shared folder.

## Tech Stack

- **Python 3.12+** with **PySide6** (Qt6)
- **uv** for dependency management
- **ruff** for linting
- **pytest** for tests
- **PyInstaller** for single-file executables

## Core Principle

Keep code minimal. Simple, readable, works.

## Architecture

~~~
chromium-profile-syncer/
├── src/
│   ├── main.py               # Entry point, single-instance check
│   ├── tray.py               # System tray app, menu, sync orchestration
│   ├── sync_engine.py        # Core sync logic, tar archive, extension detection
│   ├── settings.py           # Settings dialog UI (with Clean button)
│   ├── config.py             # Config persistence (JSON)
│   ├── autostart.py          # Platform-specific autostart registration
│   ├── single_instance.py    # File-based locking
│   ├── log_viewer.py         # GUI log handler
│   ├── shortcuts_editor.py   # Search shortcuts editing dialog
│   ├── dracula.py            # Dark theme stylesheet
│   └── browsers/             # One file per browser
│       ├── base.py           # Abstract browser class
│       ├── thorium.py
│       ├── helium.py
│       ├── chrome.py
│       ├── yandex.py
│       └── __init__.py       # ALL_BROWSERS list
├── tests/
├── pyproject.toml
└── README.md
~~~

## Browser Module Pattern

Each browser file defines:
- Profile paths per OS
- How to detect if installed
- How to detect if running
- Profile discovery (single vs multiple profiles)

Base class handles the actual sync operations (shared logic).

## Supported Browsers

### Thorium
- **Windows:** `%LOCALAPPDATA%\Thorium\User Data`
- **macOS:** `~/Library/Application Support/Thorium`
- **Linux:** `~/.config/thorium`

### Helium (by imput)
- **Windows:** `%LOCALAPPDATA%\imput\Helium\User Data`
- **macOS:** `~/Library/Application Support/net.imput.helium`
- **Linux:** `~/.config/net.imput.helium`

### Chrome
- **Windows:** `%LOCALAPPDATA%\Google\Chrome\User Data`
- **macOS:** `~/Library/Application Support/Google/Chrome`
- **Linux:** `~/.config/google-chrome`

### Yandex
- **Windows:** `%LOCALAPPDATA%\Yandex\YandexBrowser\User Data`
- **macOS:** `~/Library/Application Support/Yandex/YandexBrowser`
- **Linux:** `~/.config/yandex-browser`

More browsers can be added via the plugin pattern (one file per browser).

## Chromium Profile Structure — Research Findings

All Chromium-based browsers share the same profile structure under `User Data/`:

~~~
User Data/
├── Default/                  # or "Profile 1", "Profile 2", etc.
│   ├── Bookmarks             # Plain JSON with GUIDs — mergeable
│   ├── Custom Dictionary.txt # Plain text, one word per line
│   ├── Extensions/           # Extension code
│   │   └── <ext-id>/
│   │       └── <version>/    # Full extension source
│   ├── Local Extension Settings/  # Extension localStorage (LevelDB)
│   │   └── <ext-id>/
│   ├── Extension Settings/   # Extension sync storage (LevelDB)
│   │   └── <ext-id>/
│   ├── Local Storage/        # Website localStorage (LevelDB)
│   │   └── leveldb/
│   ├── Web Data              # SQLite — search engines + encrypted payment data
│   ├── Preferences           # JSON — browser settings
│   ├── Secure Preferences    # JSON — HMAC-protected settings (machine-specific)
│   ├── Login Data            # SQLite — ENCRYPTED passwords
│   ├── Cookies               # SQLite — ENCRYPTED cookies
│   └── History               # SQLite — browsing history
~~~

### What to Sync

| Data Type              | Location                         | Format       | Syncable?                                           |
| ---------------------- | -------------------------------- | ------------ | --------------------------------------------------- |
| Extensions (unpacked)  | `Extensions/<id>/<version>/`     | Files        | YES (only developer/unpacked extensions)            |
| Extensions (Web Store) | `Extensions/<id>/<version>/`     | Files        | ID ONLY (auto-download from Chrome Web Store)       |
| Extension settings     | `Local Extension Settings/<id>/` | LevelDB      | YES                                                 |
| Extension sync storage | `Extension Settings/<id>/`       | LevelDB      | YES                                                 |
| Bookmarks              | `Bookmarks`                      | JSON         | YES                                                 |
| Custom dictionary      | `Custom Dictionary.txt`          | Plain text   | YES                                                 |
| Local Storage          | `Local Storage/leveldb/`         | LevelDB      | YES                                                 |
| Search shortcuts       | `Web Data` (keywords table)      | SQLite → JSON | YES (user-created engines only; extracted to search_shortcuts.json) |

### What NOT to Sync (encrypted / sensitive)

| Data Type       | Location                        | Why Not                              |
| --------------- | ------------------------------- | ------------------------------------ |
| Passwords       | `Login Data`                    | OS-level encryption (DPAPI/Keychain) |
| Cookies         | `Cookies`                       | OS-level encryption                  |
| Payment methods | `Web Data` (credit_cards table) | OS-level encryption                  |
| History         | `History`                       | User explicitly excluded             |

### Important Notes

- **Secure Preferences** has HMAC signatures that are machine-specific. Chrome/Chromium
  will reset tampered values on launch. This is fine — the browser recovers gracefully,
  but it means some settings may not transfer perfectly.
- **Web Data** is a shared SQLite file containing both search engines (`keywords` table)
  and encrypted credit card data. Only user-created search engines (`prepopulate_id = 0`)
  are extracted and stored in `search_shortcuts.json`; the file itself is never copied.
- **LevelDB directories** must be copied as complete directories (all files together) or
  not at all. Partial copies corrupt the database.
- **Extension code** uses manifest.json with a `"version"` field that can be compared for
  conflict resolution.

### Smart Extension Syncing (Space Optimization)

**Problem:** Syncing full extension code wastes massive space:
- Web Store extensions can be re-downloaded
- Only unpacked/developer extensions can't be re-downloaded

**Solution:**
1. **Detect extension source:**
   - Web Store extensions have `_metadata/verified_contents.json` (signed by Google)
   - Unpacked extensions don't have this file
2. **Sync strategy:**
   - **Web Store extensions:** Save extension ID to `webstore_extensions.json` manifest
   - **Unpacked extensions:** Sync full code (can't re-download)
3. **Auto-installation:**
   - Generate External Extensions JSON stubs from manifest
   - Browser auto-downloads Web Store extensions on next launch

**Space savings:** ~1.1GB (80% reduction)

### Trash File Exclusion

Files excluded from sync (waste space, no value):
- `._*` — macOS metadata files on exFAT/FAT32 drives

**Implementation:**
- rclone: `--exclude "._*"`
- shutil.copytree: `ignore=shutil.ignore_patterns("._*")`

## Settings Window

- Sync folder path + browse button + clean button
  - **Clean button:** Visible only when sync folder has data. Deletes all synced data
    (`current.tar`, `metadata.json`, `search_shortcuts.json`) and clears config to start fresh.
    Shows confirmation dialog before deletion.
- List of detected browsers with checkboxes
  - If browser has multiple profiles: expandable list with profile checkboxes
  - If browser has one profile: just the browser checkbox (no expand)
- "What to sync" section: checkboxes for each syncable data type (all on by default)
- Autostart checkbox (on by default)
- Tooltip explaining: "Passwords, cookies, payment methods, and history cannot be synced
  due to encryption"
- Fixed width (520px), auto-adjusting height, non-resizable

## Tray Behavior

### Menu

~~~
Sync Now
Settings
─────────────
Status line (dynamic)
─────────────
Quit
~~~

### Status Line States

- `"Initial setup complete"` — after first sync (no timestamp spam)
- `"Last sync: [ISO timestamp]"` — regular syncs
- `"Syncing: [file/folder name]"` — real-time progress
- `"Idle"` — default state

### Icon States

Simple 22x22 colored circles generated programmatically:
- **Idle** — Gray (#808080)
- **Syncing** — Blue (#4A90D9)
- **Waiting** — Orange (#E8A317) — browser running, waiting to sync
- **Error** — Red (#D94A4A)

### Progress Display

Progress shown in tray menu (no separate window):
- Icon color changes during sync
- "Sync Now" button shows current operation: "⏳ [truncated filename]"
- Status line shows full operation: "Syncing: [full filename/path]"

### Button States During Sync

- "Sync Now" → disabled, text shows progress: "⏳ [current operation]"
- "Settings" → disabled (prevents config changes mid-sync)
- Re-enabled when sync completes

## Sync Logic

### Triggers

1. File watcher on sync folder (detects incoming changes from other PCs)
2. Periodic timer (configurable: 1, 5, 10, 15, 30 min, 1 hour; default 15 min)
3. Manual "Sync Now" click

### Direction

Bidirectional ("both"), push-only, or pull-only — configurable per profile.

### Browser Running Check

If a browser is running: skip that browser, update tray status to
"Waiting for X to close...", sync happens on next trigger when browser is closed.

### Conflict Resolution

| Data Type          | Strategy                                     |
| ------------------ | -------------------------------------------- |
| Extension code     | Higher manifest.json `version` wins          |
| Extension settings | Last-write-wins by file mtime, per-extension |
| Bookmarks          | Last-write-wins                              |
| Everything else    | Last-write-wins by file mtime                |

### Sync Folder Layout

~~~
sync-folder/
├── current.tar              # Single tar archive of all synced profile data
├── metadata.json            # Timestamps, version info, first-sync flag
└── search_shortcuts.json    # Custom search engines (shared across browsers)
~~~

All profile data is packed into `current.tar` before syncing. Cloud sync clients
(Syncthing, Dropbox) only ever see one file change per sync cycle, preventing partial syncs.

**Packing:** Writes to a temp file outside the sync folder, then moves atomically.
**Unpacking:** Extracts to a system temp directory; cloud client never sees individual profile files.

**First-sync detection:**
- If `metadata.json` doesn't exist → first-time setup (shows "Initial setup complete")
- If `metadata.json` exists → regular sync (shows "Last sync: timestamp")

## Search Shortcuts

Custom search engines are extracted from `Web Data` (SQLite, `keywords` table) and stored
as `search_shortcuts.json` at the sync folder root. Only `prepopulate_id = 0` rows
(user-created) are saved. Built-in engines are reinstalled by the browser.

**Windows-specific:** Chromium validates a `url_hash` BLOB for every keywords row on startup.
Missing or invalid hash → row silently dropped. The hash is AES-256-GCM encrypted using the
OSCrypt key from `Local State`. See `docs/search-shortcuts.md` for full formula and implementation.

**sync_guid rules:**
- Default search engine: `sync_guid` must match `Preferences["default_search_provider"]["guid"]`
- All other user engines: `sync_guid = ""` (local-only; Chrome deletes unknown UUIDs during sync reconciliation)

## First Run

1. App opens settings window automatically
2. User picks sync folder
3. If folder contains existing sync data → import automatically, no prompt
4. If folder is empty → start fresh

## Theming

- **Windows/macOS:** Force dark theme (Dracula)
- **Linux:** Use system Qt theme

## Autostart

Register app to start on login. On by default, toggleable in settings.

Platform-specific implementation:
- **Windows:** Registry key (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`)
- **macOS:** LaunchAgent plist at `~/Library/LaunchAgents/`
- **Linux:** XDG autostart `.desktop` file in `~/.config/autostart/`

## Platform Paths Summary

| Browser | Windows                                        | macOS                                            | Linux                           |
| ------- | ---------------------------------------------- | ------------------------------------------------ | ------------------------------- |
| Thorium | `%LOCALAPPDATA%\Thorium\User Data`             | `~/Library/Application Support/Thorium`          | `~/.config/thorium`             |
| Helium  | `%LOCALAPPDATA%\imput\Helium\User Data`        | `~/Library/Application Support/net.imput.helium` | `~/.config/net.imput.helium`    |
| Chrome  | `%LOCALAPPDATA%\Google\Chrome\User Data`       | `~/Library/Application Support/Google/Chrome`    | `~/.config/google-chrome`       |
| Yandex  | `%LOCALAPPDATA%\Yandex\YandexBrowser\User Data`| `~/Library/Application Support/Yandex/YandexBrowser` | `~/.config/yandex-browser`  |

## Deliverables

✅ Working app with all features described above
✅ 119 passing tests for sync logic, browsers, config, settings
✅ README with usage instructions
✅ `pyproject.toml` with uv/ruff configured
✅ `build.py` script with `--install` flag (PyInstaller)
✅ `Makefile` with `install` and `ci` targets
✅ `CLAUDE.md` with lockfile rules and project conventions
✅ Smart extension syncing (Web Store vs unpacked)
✅ Tar archive syncing (single `current.tar`, no backup rotation folders)
✅ Real-time progress in tray menu (no separate window)
✅ First-sync detection (no spam on clean slate)
✅ Trash file exclusion (`._*` macOS metadata)
✅ Space optimization: ~80% reduction
✅ Clean button to start fresh (deletes all synced data)
✅ Search shortcuts sync with Windows url_hash support

## Implementation Notes

- **PySide6** (not PyQt6) — binding explicitly required
- LevelDB directories must be copied atomically (all files or none)
  - Uses `shutil.copytree` with atomic rename via `.tmp` staging directory
  - Ignores `._*` files via `ignore_patterns()`
- Browser detection: check if the profile directory exists on disk
- Running detection: uses `psutil` to check process list for browser executable name
- Settings persistence: JSON config file at platform-specific config directory
- The sync folder is the user's responsibility to set up (NAS, Dropbox, Syncthing, etc.)
- The app just reads/writes to a local path

## Tech Stack

**Runtime dependencies:**
- `PySide6 >= 6.7` — Qt6 bindings
- `psutil >= 5.9` — Cross-platform process detection
- `cryptography >= 46.0.7` — Search shortcuts url_hash computation (Windows)
- `rclone` — System binary, required for progress reporting

**Single-instance enforcement:**
- Uses file locking to prevent multiple app instances
- Lock file at platform config directory

## File Structure (Actual Implementation)

~~~
chromium-profile-syncer/
├── src/
│   ├── main.py              # Entry point, single instance check
│   ├── tray.py              # System tray app, menu, sync orchestration
│   ├── sync_engine.py       # Core sync logic, tar archive, extension detection
│   ├── settings.py          # Settings dialog UI (with Clean button)
│   ├── config.py            # Config persistence (JSON)
│   ├── autostart.py         # Platform-specific autostart registration
│   ├── single_instance.py   # File-based locking
│   ├── log_viewer.py        # GUI log handler
│   ├── shortcuts_editor.py  # Search shortcuts editing dialog
│   ├── dracula.py           # Dark theme stylesheet
│   └── browsers/
│       ├── base.py          # Abstract BrowserBase class
│       ├── thorium.py       # Thorium browser implementation
│       ├── helium.py        # Helium browser implementation
│       ├── chrome.py        # Chrome browser implementation
│       ├── yandex.py        # Yandex browser implementation
│       └── __init__.py      # ALL_BROWSERS list
├── tests/
│   ├── test_sync_engine.py  # 58 tests
│   ├── test_browsers.py     # 13 tests
│   ├── test_settings.py     # 20 tests
│   ├── test_config.py       # 8 tests
│   ├── test_autostart.py    # 12 tests
│   └── test_tray.py         # 15 tests
├── build.py                 # PyInstaller build script with --install
├── Makefile                 # install, ci targets
├── pyproject.toml           # uv deps, ruff config, pytest config
├── DESIGN.md                # This file
├── CLAUDE.md                # Project conventions for AI assistants
├── docs/
│   └── search-shortcuts.md  # Deep dive: Windows url_hash, sync_guid rules
└── README.md                # Usage instructions
~~~
