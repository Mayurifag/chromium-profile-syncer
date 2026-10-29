# chromium-profile-syncer

Desktop tray app for syncing Chromium profiles through a shared folder, including cloud folders such as OpenCloud.
Supports Windows, macOS and Linux, with browser definitions for Chrome, Helium, Thorium and Yandex.

Browsers must be fully closed before syncing. The app watches for browser closure, shared-folder changes and its
periodic sync interval. Keep an independent backup before trying a new build: this software writes browser profile data.

## Extension management

Manage extension removal and disabling in the browser normally. The syncer no longer creates forced-install policies or
writes protected extension preferences.

- **Active:** shared install list and backed-up settings.
- **Saved:** removed/archived extension, with its last backed-up settings retained on disk. It is excluded from
  installation and settings restoration.
- **Archive:** move an extension to Saved across all synced browsers. Existing copies in other browsers cannot silently
  reactivate the shared record.
- **Restore:** return it to Active and open its Web Store page after saving. Install/confirm it in the browser, then
  close the browser to restore settings.

The Extensions dialog supports searching, Archive/Restore, direct Web Store links and saved-settings sizes. Cancel
discards pending changes. Opening the dialog does not delete or prune anything, scrape Web Store names or generate HTML
files.

### Removal and disabling

After a successful sync, the app remembers which extensions were installed in each local profile. If an extension
disappears on a later sync, it becomes Saved globally. A new empty profile does not count as a mass removal. Disabled
extensions remain installed and are not archived; their browser-local enabled state is left alone.

The saved settings are the **last successful backup**, not data recovered after uninstall. If an extension was never
backed up, uninstalling it may lose its settings. Saved entries are logical archive records; their data stays in the
shared backup rather than being deleted or copied back into profiles.

### Installation limits

The app registers active Web Store IDs using removable external-extension entries: per-user registry keys on Windows and
JSON stubs on macOS/Linux. Support varies by browser; Windows/macOS may require confirmation, and some browsers require
manual installation. Reinstalling an extension previously rejected/uninstalled by the user may also require its Web
Store page.

Archiving removes matching external registration. Browsers can then remove externally installed copies on restart.
**Manually installed copies may still need removal in each browser.** Desktop-only mode cannot safely force universal
uninstall or change protected enable/disable state. Chrome and Thorium share Windows registry paths, so their external
install lists are not isolated.

### Upgrading from forced installation

On the first extension-registration pass for each browser, the app releases its legacy policies. Windows cleanup removes
only bare force-list values matching known saved/active IDs; it does not wipe shared registry keys. Linux cleanup
removes only `<browser>-syncer.json` and requires `pkexec` authorization. macOS cleanup targets only the syncer's own
configuration-profile identifiers and may require manual removal in System Settings. Failures are reported and retried;
successful cleanup is recorded locally and is not repeated on every sync.

Unrelated or URL-bearing policies are left alone. If an extension is still managed, inspect the browser's policy page
and remove the responsible policy through its owner. Fully restart the browser after migration.

## Synced data

- Web Store extension IDs/names and extension settings (Local/Sync Extension Settings and extension IndexedDB, with
  databases copied unchanged rather than parsed and rewritten to strip caches). Backups may be larger, but no database
  records are deliberately discarded.
- Bookmarks, custom dictionary, local storage and selected unencrypted preferences.
- User-created search shortcuts, omnibox shortcuts and typed URLs.
- Browser flags and theme preferences.

Passwords, cookies, payment/autofill data, sessions, full browsing history and favicons are excluded. Extension
executable files are not automatically restored; Web Store extensions are fetched by the browser.

Search shortcuts are shared between browsers. Restoration intentionally replaces built-in engines with the saved custom
engines. Windows requires valid encrypted URL hashes; see [search shortcut implementation](docs/search-shortcuts.md).

Restoration copies selected data into staging before replacing live files and databases. App metadata, saved-library
records and archived extension data are not copied into browser profiles. Disabled data types are not restored.

## Build and checks

Requirements: Python 3.12+ and [uv](https://docs.astral.sh/uv/). No rclone is needed.

```sh
make ci
make install
```

`make install` builds, stops the existing app, installs it and launches it. Install locations are
`%LOCALAPPDATA%\Programs\chromium-profile-syncer` on Windows, `~/Applications` on macOS and `~/.local/bin` on Linux.

App settings live in `%APPDATA%\chromium-profile-syncer\config.json` on Windows and
`$XDG_CONFIG_HOME/chromium-profile-syncer/config.json` (default `~/.config/chromium-profile-syncer/config.json`)
elsewhere. Shared data lives in `current/` inside the chosen sync folder. Local extension observations stay in app
settings, not the shared folder.

GUI and CLI share local operation locks. Paused profiles are excluded from automatic sync; explicit Sync and Restore
actions can still run. Extension Restore reapplies only the selected extensions' settings after installation. Browser
updating, including Helium/winget integration, is not part of this app.

Shared snapshots are staged, checked for conflicting changes and published with checksums. Incomplete cloud downloads
are rejected rather than used for restoration. Desktop archive restoration validates and stages the archive before
replacing the existing backup. These protections are not a distributed lock or a replacement for independent backups.

### CLI

The packaged executable and `uv run python -m src.main` accept the same subcommands:

```sh
uv run python -m src.main browsers
uv run python -m src.main sync --browser Chrome --profile Default
uv run python -m src.main restore --browser Chrome --profile Default
uv run python -m src.main config show
```

`remove-profile --browser Chrome --profile Default --yes` deletes only that local profile, not the shared backup. The
old standalone CLI and destructive browser E2E scripts have been removed. `make e2e` runs disposable-fixture lifecycle
tests without changing installed browsers or real app settings.

Optional extension filters in app settings:

- `ungoogled_only_extensions`: install only in ungoogled browsers such as Helium.
- `windows_only_extensions`: install only on Windows.
- `excluded_ext_settings_ids`: exclude settings for selected extensions.

## Network and privacy

Extension names come from local manifests; the Extensions dialog makes no background Web Store requests. Web Store links
open only when requested or when saving a Restore action. Browser-managed installation/update traffic still occurs. The
packaged app checks GitHub releases for its own updates. It does not check or update browser versions. Local logs and
sync progress remain enabled.

## TODO / future work

- Optional companion browser extension for live change tracking and supported enable/disable/uninstall actions. Keep
  desktop-only mode available.
- Companion-assisted cross-browser removal of manually installed extensions, with browser confirmation where required.
- Explicit target-browser/profile selection for restoring saved extensions.
- Test removable external installation and legacy-policy migration end to end on every supported browser and operating
  system.
- Consider a verified extension-package cache for faster/offline reinstalls, without bypassing signatures, permissions
  or browser installation controls.
- Recovery/version history for simultaneous writes from multiple machines and cloud-provider conflict copies; current
  snapshot conflict detection is not a distributed lock.
- Settings snapshots/version history and an explicit permanent-purge action for Saved extensions. Archive must remain
  non-destructive by default.
- Revisit search-engine default reconciliation across Chromium forks; document whether the `gru` shortcut can be
  selected automatically in each browser.
