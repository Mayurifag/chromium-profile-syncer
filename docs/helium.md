# Helium Browser — Known Issues on Windows

Helium (imput's browser, `%LOCALAPPDATA%\imput\Helium`) is based on Chromium 146+
but behaves differently from Chrome/Thorium in several ways that break sync.
All issues below were confirmed on Windows.

---

## 1. Webstore extension auto-install does not work

Both mechanisms that work for Chrome/Thorium fail for Helium on Windows.

### File-based stubs (`External Extensions\*.json`)

Writing `{"external_update_url": "..."}` stubs to
`%LOCALAPPDATA%\imput\Helium\User Data\External Extensions\`
has no effect. Helium (Chromium 146) ignores the file-based external extension
directory on Windows entirely. After launch, `Default\Extensions\` remains absent
and `extensions.settings` in Preferences stays empty.

### Registry-based (`HKCU\SOFTWARE\Chromium\Extensions`)

The registry key `HKCU\SOFTWARE\Chromium\Extensions` exists (Helium creates it)
and the matching force-list key
`HKCU\SOFTWARE\Policies\Chromium\ExtensionInstallForcelist` also exists.
Writing extension IDs to these keys (the standard Chromium mechanism) was tested
but extensions still did not install — `Default\Extensions\` remained absent after
relaunch. It is unclear whether Helium reads from these keys or ignores them
entirely due to its ungoogled build flags.

### Consequence

There is currently no known automated mechanism to install webstore extensions
into Helium via profile sync on Windows. Extensions must be installed manually
from within Helium after applying a backup.

---

## 2. Helium's built-in uBlock Origin is not removed on apply-backup

Helium ships uBlock Origin (and other extensions) as internal/component
extensions. When applying a backup (restore), the sync tool:

- Deletes `Default\Extensions\`
- Deletes `Default\Local Extension Settings\`
- Clears `extensions.settings` from `Default\Preferences`

Despite this, uBlock Origin reappears after the next Helium launch. Helium
re-injects its bundled extensions unconditionally — presumably from a manifest
inside its installation directory — so profile-level cleanup has no effect on them.

**This cannot be fixed at the sync-tool level.** To remove bundled extensions,
disable them manually from within `helium://extensions` after launching.

---

## 3. Default search engine is not removed on apply-backup

When applying a backup, the sync tool restores user-created search shortcuts
(`prepopulate_id = 0`) and updates `default_search_provider.guid` in Preferences.
However, Helium resets the default search engine to its own built-in default on
next launch if it does not recognise the GUID stored in Preferences.

Additionally, the backup's `search_shortcuts.json` may not have a matching entry
for the exact search engine Helium uses as its default (different `prepopulate_id`
or different URL template), so the round-trip fails silently.

**Result:** After applying a backup and launching Helium, the default search engine
reverts to whatever Helium considers its own default, regardless of what was
restored.

---

## Summary

| Feature | Status |
|---|---|
| Extension install via file stubs | ✗ Not working |
| Extension install via registry | ✗ Not working (tested) |
| Built-in extension (uBlock) removal | ✗ Not possible — Helium re-injects |
| Default search engine restore | ✗ Helium overrides on launch |
| Bookmarks sync | ✓ Works |
| Custom Dictionary sync | ✓ Works |
| Local Storage sync | ✓ Works |
| Preferences (partial) sync | ✓ Works |
