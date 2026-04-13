 # chromium-profile-syncer

Highly vibecoded! Unstable! Proof of concept! Dont use (yet)!

Cross-platform (Windows/MacOS/Linux) Chromium-based browsers bidirectional
profiles sync system to use with cloud folders (I use selfhosted OpenCloud).

## Supported Browsers

- **Thorium**
- **Helium**
- **Chrome**
- **Yandex**

The architecture supports adding more Chromium-derived browsers with minimal
changes (one file per browser).

## What Gets Synced

| Data                             | Synced                                   |
| -------------------------------- | ---------------------------------------- |
| Bookmarks                        | Yes                                      |
| Extensions (unpacked)            | Yes — full code synced                   |
| Extensions (Web Store)           | IDs only — with autoinstall              |
| Extension settings/storage/cache | Yes (not 100% sure works for everything) |
| Local Storage                    | Yes                                      |
| Preferences                      | Yes — unencrypted ones                   |
| Custom Dictionary                | Yes                                      |
| Search Shortcuts                 | Yes — user-created engines only          |
| Themes                           | Yes — only active one                    |

### Search Shortcuts

User-created search engines (custom shortcuts in the omnibox) are extracted from
the browser's `Web Data` SQLite database and stored as `search_shortcuts.json`
in the sync folder root. This file is shared across all browsers syncing to the
same folder.

Built-in engines (Google, Bing, etc.) are never backed up.

### What Does NOT Sync

Passwords, cookies, payment data, and browsing history are
**intentionally excluded**. These are protected by OS-level encryption (e.g.
macOS Keychain, Windows DPAPI) that ties the data to a specific machine.

Sessions, autofill data/profiles, omnibox shortcuts - no need, annoying for my
use cases.

Favicons - not worth syncing.

### Trash Files Excluded

Files automatically excluded from sync (waste space, no value):
- `._*` — macOS metadata files on exFAT/FAT32 drives

## Installation

### Build from Source

Requirements: **Python 3.12+**, **[uv](https://docs.astral.sh/uv/)**, **rclone**

~~~sh
make install
~~~

**What `install` does:**
1. Builds the executable/bundle
2. Kills any running instance
3. Installs to platform-specific location
4. Launches the app
5. **(macOS only)** Removes Gatekeeper quarantine

### Sync Folder Layout

~~~
sync-folder/
├── current.tar              # Compressed tar archive of all synced profile data
├── metadata.json            # Sync timestamps and version info
└── search_shortcuts.json    # Custom search engines (shared across browsers)
~~~

### Key Design Decisions

#### Search shortcuts

Extracts user-created search engines from `Web Data` SQLite
(`prepopulate_id = 0` only). On Windows, computes mandatory `url_hash` BLOB
(AES-256-GCM over SHA-256 of a Chromium Pickle); rows without a valid hash are
silently dropped at startup. See
[docs/search-shortcuts.md](./docs/search-shortcuts.md) for full implementation
details.

#### Profile saved into single file archive

Profile intentionally saved in `.tar` archive to compress files and operate only
one file, so there will be less `inotify` events in clouds (in other words
clouds prefer one big file, than thousands small ones).

#### Extensions installation system

It's impossible to have identically working extensions installing system across
all browsers and it also may conflict. For example, Thorium and Chrome use the
same registry paths to look for extensions, so if you start managing Thorium,
extensions will also install to all used profiles of Chrome.

For Windows extensions installed via registry keys, Linux/MacOS via external
extensions `.json` files (no other ways to manage those).
