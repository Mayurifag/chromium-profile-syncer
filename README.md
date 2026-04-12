 # chromium-profile-syncer

A cross-platform PySide6 system tray application that syncs Chromium browser profiles between machines via a shared folder (e.g. Syncthing, Dropbox, or a network share).

**Smart syncing:** Only syncs unpacked/developer extensions (full code). Web Store extensions are registered by ID and auto-download fresh on new machines — saves ~1.1GB (80% space reduction).

## Supported Browsers

- **Helium** — Chromium-based browser by imput

The architecture supports adding more Chromium-derived browsers with minimal changes (one file per browser).

## What Gets Synced

| Data                       | Synced                                                |
| -------------------------- | ----------------------------------------------------- |
| Bookmarks                  | Yes                                                   |
| Extensions (unpacked)      | Yes — full code synced                                |
| Extensions (Web Store)     | ID only — auto-downloads from Chrome Web Store        |
| Extension settings/storage | Yes                        |
| Local Storage              | Yes                        |
| Preferences                | Yes                        |
| Custom Dictionary          | Yes                        |
| Web Data (search engines)  | No (skipped for v1)        |

### Smart Extension Syncing

**Web Store extensions** (e.g., uBlock Origin, React DevTools):
- Only extension ID is saved to `webstore_extensions.json`
- Browser auto-downloads from Chrome Web Store on next launch
- **Saves ~1.1GB** across current + 2 backups (80% space reduction)

**Unpacked/developer extensions:**
- Full code is synced (can't be re-downloaded)
- Detected by absence of `_metadata/verified_contents.json`

### What Does NOT Sync

Passwords, cookies, payment data, and browsing history are **intentionally excluded**. These are protected by OS-level encryption (e.g. macOS Keychain, Windows DPAPI) that ties the data to a specific machine. Copying them as-is to another machine would either fail to decrypt or cause data corruption.

### Trash Files Excluded

Files automatically excluded from sync (waste space, no value):
- `._*` — macOS metadata files on exFAT/FAT32 drives

## Installation

### Requirements

**Runtime dependency:** [rclone](https://rclone.org/) — Fast file sync tool
- **macOS:** `brew install rclone`
- **Linux:** `sudo apt install rclone` or `sudo pacman -S rclone`
- **Windows:** Download from [rclone.org/downloads](https://rclone.org/downloads/)

### Download Pre-built Binary

Download the latest release for your platform from the [Releases](../../releases) page.

> **Note:** PyInstaller `.app` bundles the entire PySide6 runtime and are typically **80–120 MB**. Each platform (macOS, Windows, Linux) requires its own native build — cross-compilation is not supported.

#### macOS

1. Install rclone: `brew install rclone`
2. Download `chromium-profile-syncer.app`
3. Move to `/Applications` or `~/Applications`
4. Right-click → **Open** → confirm (Gatekeeper warning on first launch)

Or remove quarantine flag:
~~~sh
xattr -cr chromium-profile-syncer.app
~~~

### Build from Source

Requirements: **Python 3.12+**, **[uv](https://docs.astral.sh/uv/)**, **rclone**

~~~sh
git clone <repo-url>
cd chromium-profile-syncer

# Install rclone first (choose your platform)
brew install rclone              # macOS
sudo apt install rclone          # Linux (Debian/Ubuntu)
sudo pacman -S rclone            # Linux (Arch)
# Windows: Download from https://rclone.org/downloads/
~~~

~~~sh
# Build and install (all platforms)
make install

# Or manual build
uv sync --group dev
uv run python build.py --install
~~~

**Build output locations:**
- **macOS:** `dist/chromium-profile-syncer.app`
- **Windows:** `dist/chromium-profile-syncer.exe`
- **Linux:** `dist/chromium-profile-syncer`

**Install locations:**
- **macOS:** `~/Applications/chromium-profile-syncer.app`
- **Windows:** `%LOCALAPPDATA%\Programs\chromium-profile-syncer\chromium-profile-syncer.exe`
- **Linux:** `~/.local/bin/chromium-profile-syncer`

**What `install` does:**
1. Builds the executable/bundle
2. Kills any running instance
3. Installs to platform-specific location
4. Launches the app
5. **(macOS only)** Removes Gatekeeper quarantine

## Usage

1. Launch the executable — the app appears in the system tray.
2. On first run, the Settings dialog opens automatically.
3. Configure the **sync folder** (the shared directory that holds synced profile data).
4. Enable the browsers you want to sync.
5. Use the tray icon menu to trigger a manual sync or open Settings.

The app can optionally start at login (configure in Settings).

### Starting Fresh

If you want to clear all synced data and start over:
1. Open **Settings**
2. Click the **Clean** button (appears next to Browse when sync folder has data)
3. Confirm deletion
4. All synced data (current/, backup-1/, backup-2/, metadata.json) will be deleted
5. You can then choose a new profile to upload

### Tray Menu

- **Sync Now** — Manually trigger sync
- **Settings** — Configure browsers, sync folder, data types
- **Status line** — Shows last sync timestamp or current operation
- **Quit** — Exit the app

### Progress Display

The tray menu and icon show real-time sync status:
- Icon color changes (gray → blue → gray/orange/red)
- "Sync Now" button shows current operation during sync
- Status line updates with file/folder names being synced
- rclone progress percentages: "Creating backup (45%)"

### Tray Icon States

- **Gray** — Idle
- **Blue** — Syncing
- **Orange** — Waiting for browser to close
- **Red** — Error

## Development

~~~sh
# Install all dependencies including dev tools
uv sync --group dev

# Run tests + linting (CI)
make ci

# Run tests only
uv run pytest

# Lint only
uv run ruff check src tests

# Build and install
make install

# Run from source
uv run python -m src.main
~~~

### Test Coverage

119 passing tests across:
- `test_sync_engine.py` — 58 tests (core sync logic, smart extension detection)
- `test_browsers.py` — 13 tests (browser detection, profile discovery)
- `test_settings.py` — 20 tests (config persistence, UI)
- `test_config.py` — 8 tests (JSON config read/write)
- `test_autostart.py` — 12 tests (platform-specific autostart)
- `test_tray.py` — 15 tests (tray app orchestration)

## Architecture

~~~
src/
├── main.py              # Entry point, single-instance check
├── tray.py              # System tray app, menu, sync orchestration
├── sync_engine.py       # Core sync logic, smart extension detection
├── settings.py          # Settings dialog UI (with Clean button)
├── config.py            # Config persistence (JSON)
├── autostart.py         # Platform-specific autostart registration
├── single_instance.py   # File-based locking
└── browsers/
    ├── base.py          # Abstract BrowserBase class
    ├── helium.py        # Helium browser implementation
    └── __init__.py      # ALL_BROWSERS list
~~~

### Key Design Decisions

**Smart extension syncing:**
- Detects Web Store extensions via `_metadata/verified_contents.json` signature
- Only syncs unpacked/developer extensions (full code)
- Web Store extensions registered by ID in manifest, auto-download on new machines

**Backup rotation:**
- Uses rclone for fast parallel file transfer (8 parallel transfers)
- Parses `--stats-one-line` for real-time progress percentages
- Automatic trash file exclusion via `--exclude "._*"`

**First-sync detection:**
- Checks for `metadata.json` existence
- First sync shows "Initial setup complete" (no timestamp spam)
- Subsequent syncs show "Last sync: [ISO timestamp]"

**Config locations:**
- **macOS/Linux:** `~/.config/chromium-profile-syncer/config.json`
- **Windows:** `%APPDATA%\chromium-profile-syncer\config.json`

**Lock file:**
- **macOS/Linux:** `~/.config/chromium-profile-syncer/app.lock`
- **Windows:** `%APPDATA%\chromium-profile-syncer\app.lock`
