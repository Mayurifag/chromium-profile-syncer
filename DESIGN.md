# Chromium Profile Syncer — Design Document

## Overview

Cross-platform PySide6 system tray application that syncs Chromium browser profiles
(extensions, settings, bookmarks) across machines via a shared folder.

## Tech Stack

- **Python 3.12+** with **PySide6** (Qt6)
- **uv** for dependency management
- **ruff** for linting
- **pytest** for tests
- **PyInstaller** for single-file executables
- Can use system-installed **ripgrep** for fast file scanning

## Core Principle

Keep code minimal. This is v1 — no edge case handling unless obvious. Simple, readable, works.

## Architecture

~~~
chromium-profile-syncer/
├── src/
│   ├── main.py           # Entry point, tray app
│   ├── sync_engine.py    # Core sync logic
│   ├── settings.py       # Settings UI + persistence
│   ├── watcher.py        # File watcher for sync folder
│   ├── browsers/         # One file per browser
│   │   ├── base.py       # Abstract browser class
│   │   ├── thorium.py
│   │   ├── helium.py
│   │   └── ...           # Easy to add more
│   └── icons/            # Tray icons for states
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

## Supported Browsers (at launch)

### Thorium
- **Windows:** `%LOCALAPPDATA%\Thorium\User Data`
- **macOS:** `~/Library/Application Support/Thorium`
- **Linux:** `~/.config/thorium`

### Helium (by imput)
- **Windows:** `%LOCALAPPDATA%\imput\Helium\User Data`
- **macOS:** `~/Library/Application Support/net.imput.helium`
- **Linux:** `~/.config/net.imput.helium`

More browsers can be added later via the plugin pattern (one file per browser).

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
| Extension sync storage | `Extension Settings/<id>/`       | LevelDB      | YES (renamed to "Sync Extension Settings")          |
| Bookmarks              | `Bookmarks`                      | JSON         | YES                                |
| Custom dictionary      | `Custom Dictionary.txt`          | Plain text   | YES                                |
| Local Storage          | `Local Storage/leveldb/`         | LevelDB      | YES                                |
| Search engines         | `Web Data` (keywords table)      | SQLite       | NO (skipped for v1)                |

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
  and encrypted credit card data. To sync search engines, we need to extract/import only
  the `keywords` table via SQL, not copy the whole file.
- **LevelDB directories** must be copied as complete directories (all files together) or
  not at all. Partial copies corrupt the database.
- **Extension code** uses manifest.json with a `"version"` field that can be compared for
  conflict resolution.

### Smart Extension Syncing (Space Optimization)

**Problem:** Syncing full extension code wastes massive space:
- Web Store extensions can be re-downloaded (477MB × 3 backups = 1.4GB wasted)
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

**Space savings:** ~1.1GB (80% reduction) — backups shrink from 1.6GB → 300MB

### Trash File Exclusion

Files excluded from sync (waste space, no value):
- `._*` — macOS metadata files on exFAT/FAT32 drives

**Implementation:**
- rclone: `--exclude "._*"`
- shutil.copytree: `ignore=shutil.ignore_patterns("._*")`

## Settings Window

- Sync folder path + browse button + clean button
  - **Clean button:** Visible only when sync folder has data. Deletes all synced data 
    (current/, backup-1/, backup-2/, metadata.json) and clears config to start fresh.
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
- rclone percentages shown inline: "Creating backup (45%)"

### Button States During Sync

- "Sync Now" → disabled, text shows progress: "⏳ [current operation]"
- "Settings" → disabled (prevents config changes mid-sync)
- Re-enabled when sync completes

## Sync Logic

### Triggers

1. File watcher on sync folder (detects incoming changes from other PCs)
2. Every 15 minutes (periodic)
3. Manual "Sync Now" click

### Direction

Bidirectional.

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

### Backups

Keep last 2 sync states in the sync folder. User can manually restore if needed.
Structure:

~~~
sync-folder/
├── current/                     # Active sync state
│   ├── <Browser>/
│   │   └── <Profile>/
│   │       ├── Extensions/      # Only unpacked extensions (Web Store ones excluded)
│   │       ├── Local Extension Settings/
│   │       ├── Sync Extension Settings/
│   │       ├── Bookmarks
│   │       ├── Preferences
│   │       └── webstore_extensions.json  # List of Web Store extension IDs
├── backup-1/                    # Previous state
├── backup-2/                    # State before that
└── metadata.json                # Timestamps, version info, first-sync flag
~~~

**Backup rotation uses rclone:**
- Fast parallel file transfer (8 parallel transfers, 16 checkers)
- Real-time progress reporting parsed from `--stats-one-line` output
- Automatic exclusion of trash files via `--exclude "._*"`
- Only copies changed files (incremental)

**First-sync detection:**
- If `metadata.json` doesn't exist → first-time setup (quiet, shows "Initial setup complete")
- If `metadata.json` exists → regular sync (shows "Last sync: timestamp")

## First Run

1. App opens settings window automatically
2. User picks sync folder
3. If folder contains existing sync data → import automatically, no prompt
4. If folder is empty → start fresh

## Theming

- **Windows/macOS:** Force dark theme
- **Linux:** Use system Qt theme

## Autostart

Register app to start on login. On by default, toggleable in settings.

Platform-specific implementation:
- **Windows:** Registry key or Startup folder shortcut
- **macOS:** LaunchAgent plist or Login Items
- **Linux:** XDG autostart `.desktop` file in `~/.config/autostart/`

## Platform Paths Summary

| Browser | Windows                                 | macOS                                            | Linux                        |
| ------- | --------------------------------------- | ------------------------------------------------ | ---------------------------- |
| Thorium | `%LOCALAPPDATA%\Thorium\User Data`      | `~/Library/Application Support/Thorium`          | `~/.config/thorium`          |
| Helium  | `%LOCALAPPDATA%\imput\Helium\User Data` | `~/Library/Application Support/net.imput.helium` | `~/.config/net.imput.helium` |

## Deliverables

✅ Working app with all features described above
✅ 119 passing tests for sync logic, browsers, config, settings
✅ README with usage instructions
✅ `pyproject.toml` with uv/ruff configured
✅ `build.py` script with `--install` flag for macOS (PyInstaller)
✅ `Makefile` with `install` and `ci` targets
✅ `CLAUDE.md` with lockfile rules and project conventions
✅ Smart extension syncing (Web Store vs unpacked)
✅ rclone integration for fast parallel backups
✅ Real-time progress in tray menu (no separate window)
✅ First-sync detection (no spam on clean slate)
✅ Trash file exclusion (._* macOS metadata)
✅ Space optimization: 1.6GB → 300MB (80% reduction)
✅ Clean button to start fresh (deletes all synced data)

## Open Design Questions (for implementer to decide)

These were not resolved during discussion. Implementer should make pragmatic v1 choices:

1. **Search engines sync** — The `Web Data` SQLite file contains both search engines
   (`keywords` table) and encrypted credit cards. Recommended approach: extract via SQL
   (read/write only the `keywords` table). Alternative: skip for v1.

2. **Additional browsers** — Only Thorium and Helium confirmed. The architecture supports
   adding more (Chrome, Brave, Edge, Ungoogled Chromium) via one-file modules. Implementer
   can add more if it's low effort, or ship with just two.

3. **Sync folder suggestions** — Unclear whether to detect cloud sync folders (Dropbox,
   OneDrive, Syncthing) and offer them as suggestions, or just treat it as a plain path
   picker. Recommended: plain path picker for v1.

## Implementation Notes

- **PySide6** (not PyQt6) — user explicitly corrected this
- LevelDB directories must be copied atomically (all files or none)
  - Uses `shutil.copytree` with atomic rename via `.tmp` staging directory
  - Ignores `._*` files via `ignore_patterns()`
- Browser detection: check if the profile directory exists on disk
- Running detection: uses `psutil` to check process list for browser executable name
- Settings persistence: JSON config file at `~/.config/chromium-profile-syncer/config.json`
- The sync folder is the user's responsibility to set up (NAS, Dropbox, Syncthing, etc.)
- The app just reads/writes to a local path
- **rclone dependency:** Must be installed (`brew install rclone` on macOS)
  - Used for fast parallel backup rotation
  - Parses `--stats-one-line` output for progress percentages
  - Excludes trash files automatically

## Tech Stack Additions

**Runtime dependencies:**
- `rclone` — Fast file sync tool (via Homebrew on macOS)
- `psutil` — Cross-platform process detection

**Single-instance enforcement:**
- Uses file locking to prevent multiple app instances
- Lock file: `~/.config/chromium-profile-syncer/app.lock`

## File Structure (Actual Implementation)

~~~
chromium-profile-syncer/
├── src/
│   ├── main.py              # Entry point, single instance check
│   ├── tray.py              # System tray app, menu, sync orchestration
│   ├── sync_engine.py       # Core sync logic, smart extension detection
│   ├── settings.py          # Settings dialog UI (with Clean button)
│   ├── config.py            # Config persistence (JSON)
│   ├── autostart.py         # Platform-specific autostart registration
│   ├── single_instance.py   # File-based locking
│   └── browsers/
│       ├── base.py          # Abstract BrowserBase class
│       ├── helium.py        # Helium browser implementation
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
└── README.md                # Usage instructions
~~~
