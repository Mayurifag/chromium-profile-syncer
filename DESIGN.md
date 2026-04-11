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
│   ├── IndexedDB/            # Per-origin IndexedDB data (LevelDB)
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

| Data Type              | Location                         | Format       | Syncable?                      |
| ---------------------- | -------------------------------- | ------------ | ------------------------------ |
| Extensions (code)      | `Extensions/<id>/<version>/`     | Files        | YES                            |
| Extension settings     | `Local Extension Settings/<id>/` | LevelDB      | YES                            |
| Extension sync storage | `Extension Settings/<id>/`       | LevelDB      | YES                            |
| Bookmarks              | `Bookmarks`                      | JSON         | YES                            |
| Custom dictionary      | `Custom Dictionary.txt`          | Plain text   | YES                            |
| Local Storage          | `Local Storage/leveldb/`         | LevelDB      | YES                            |
| IndexedDB              | `IndexedDB/`                     | LevelDB dirs | YES                            |
| Search engines         | `Web Data` (keywords table)      | SQLite       | YES — but needs SQL extraction |

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

## Settings Window

- Sync folder path + browse button
- List of detected browsers with checkboxes
  - If browser has multiple profiles: expandable list with profile checkboxes
  - If browser has one profile: just the browser checkbox (no expand)
- "What to sync" section: checkboxes for each syncable data type (all on by default)
- Autostart checkbox (on by default)
- Tooltip explaining: "Passwords, cookies, payment methods, and history cannot be synced
  due to encryption"

## Tray Behavior

### Menu

~~~
Sync Now
Settings
─────────
Status line (dynamic)
Quit
~~~

### Status Line States

- `"Last sync: 5 min ago"`
- `"Syncing..."`
- `"Waiting for Thorium to close..."`
- `"Never synced"`

### Icon States

Generate simple icons programmatically or include minimal SVG/PNG:
- **Idle/normal** — default state
- **Syncing** — different color or animated
- **Waiting for browser** — distinct from syncing
- **Error** — red or warning indicator

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
Structure suggestion:

~~~
sync-folder/
├── current/          # Active sync state
├── backup-1/         # Previous state
├── backup-2/         # State before that
└── metadata.json     # Timestamps, version info
~~~

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

- Working app with all features described above
- Tests for sync logic (not UI)
- README with usage instructions
- `pyproject.toml` with uv/ruff configured
- Build script or instructions for PyInstaller on each platform
- `CLAUDE.md` with lockfile rules

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
- Browser detection: check if the profile directory exists on disk
- Running detection: check process list for browser executable name
- Settings persistence: use a JSON config file in the app's data directory
- The sync folder is the user's responsibility to set up (NAS, Dropbox, Syncthing, etc.)
- The app just reads/writes to a local path
