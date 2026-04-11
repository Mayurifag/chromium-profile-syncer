# chromium-profile-syncer

A cross-platform PySide6 system tray application that syncs Chromium browser profiles (Thorium, Helium) between machines via a shared folder (e.g. Syncthing, Dropbox, or a network share).

## Supported Browsers

- **Thorium** — Chromium-based browser focused on performance
- **Helium** — Chromium-based browser

The architecture supports adding more Chromium-derived browsers with minimal changes.

## What Gets Synced

| Data                       | Synced   |
| -------------------------- | -------- |
| Bookmarks                  | Yes      |
| Extensions                 | Yes      |
| Extension settings/storage | Yes      |
| Local Storage              | Yes      |
| Preferences                | Yes      |
| Secure Preferences         | Yes      |
| Favicons                   | Yes      |
| Web Data (search engines)  | Deferred |

### What Does NOT Sync

Passwords, cookies, payment data, and browsing history are **intentionally excluded**. These are protected by OS-level encryption (e.g. macOS Keychain, Windows DPAPI) that ties the data to a specific machine. Copying them as-is to another machine would either fail to decrypt or cause data corruption.

## Installation

### Download Pre-built Binary

Download the latest release for your platform from the [Releases](../../releases) page.

> **Note:** PyInstaller `--onefile` binaries bundle the entire PySide6 runtime and are typically **80–120 MB**. Each platform (macOS, Windows, Linux) requires its own native build — cross-compilation is not supported.

#### macOS Gatekeeper

Because the binary is not notarized, macOS will block the first launch. To allow it:

~~~sh
xattr -cr ./chromium-profile-syncer
~~~

Or right-click the binary → **Open** → confirm in the dialog.

### Build from Source

Requirements: **Python 3.12+**, **[uv](https://docs.astral.sh/uv/)**

~~~sh
git clone <repo-url>
cd chromium-profile-syncer
uv sync --group dev
uv run python build.py
~~~

The executable is written to `dist/chromium-profile-syncer` (or `dist/chromium-profile-syncer.exe` on Windows).

## Usage

1. Launch the executable — the app appears in the system tray.
2. On first run, the Settings dialog opens automatically.
3. Configure the **sync folder** (the shared directory that holds synced profile data).
4. Enable the browsers you want to sync.
5. Use the tray icon menu to trigger a manual sync or open Settings.

The app can optionally start at login (configure in Settings).

## Development

~~~sh
# Install all dependencies including dev tools
uv sync --group dev

# Run tests
uv run pytest

# Lint
uv run ruff check src tests

# Run from source
uv run python -m src.main
~~~

## Architecture Notes

- Entry point: `src/main.py`
- Sync logic: `src/sync_engine.py`
- System tray UI: `src/tray.py`
- Autostart: `src/autostart.py`
- Config persistence: `src/config.py`
