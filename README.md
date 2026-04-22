 # chromium-profile-syncer

Highly vibecoded! Unstable! Proof of concept! Dont use (yet)!

Cross-platform (Windows/MacOS/Linux) Chromium-based browsers bidirectional
profiles sync system to use with cloud folders (I use selfhosted OpenCloud).

It will keep extensions (with their settings!) and some other stuff to be 
identical across my systems. Google Sync makes a little part of that - also not 
open-source and not available on Helium/Ungoogled Chromium.

## Supported Browsers

- **Thorium**
- **Helium**
- **Chrome**
- **Yandex**

The architecture supports adding more Chromium-derived browsers with minimal
changes (one file per browser) onto `src\browsers`.

## What Gets Synced

| Data                             | Synced                                   |
| -------------------------------- | ---------------------------------------- |
| Bookmarks                        | Yes                                      |
| Extensions                       | IDs only / unpacked ones sync full code  |
| Extension settings/storage/cache | Yes (not 100% sure works for everything) |
| Local Storage                    | Yes                                      |
| Preferences                      | Yes — part of unencrypted ones           |
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

Sessions, autofill data/profiles - no need, annoying for my use cases.

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

### Key Design Decisions

#### Search shortcuts

Extracts user-created search engines from `Web Data` SQLite
(`prepopulate_id = 0` only). On Windows, computes mandatory `url_hash` BLOB
(AES-256-GCM over SHA-256 of a Chromium Pickle); rows without a valid hash are
silently dropped at startup. See
[docs/search-shortcuts.md](./docs/search-shortcuts.md) for full implementation
details.

#### Extensions installation system

It's impossible to have identically working extensions installing system across
all browsers and it also may conflict. For example, Thorium and Chrome use the
same registry paths to look for extensions, so if you start managing Thorium,
extensions will also install to all used profiles of Chrome.

For Windows extensions installed via registry keys, Linux/MacOS via external
extensions `.json` files (no other ways to manage those, also not working
on browsers based on some iridium patchsets).

#### Ungoogled browsers and extension exclusions

Some browsers are marked as **ungoogled** in their definition (currently: Helium).
Ungoogled browsers strip out Google-specific built-in features such as translation,
so they need extensions to compensate (e.g. a translation extension like Linguist).
Installing those same extensions in a regular browser (Chrome, Thorium) that already
provides the feature natively would be redundant.

To mark an extension as ungoogled-only, add its Chrome Web Store extension ID to the
`ungoogled_only_extensions` list in the app config
(`%APPDATA%\chromium-profile-syncer\config.json` on Windows,
`~/.config/chromium-profile-syncer/config.json` elsewhere):

~~~json
{
  "ungoogled_only_extensions": [
    "gbefmodhlophhakmoecijeppjblibmie"
  ]
}
~~~

During restore and extension registration, these IDs are silently skipped for
non-ungoogled browsers. The backup archive always contains the extension settings
(they are sourced from whichever ungoogled browser is being synced), so the data
is preserved — it just is not applied to browsers that don't need it.

# TODO

- search shortcut gru by default - is that possible to do automatically?
- helium - extensions dont auto install - is that possible to make this work?
