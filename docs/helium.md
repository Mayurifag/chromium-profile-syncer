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

---

## 2. Helium's built-in uBlock Origin is not removed on apply-backup

Helium ships uBlock Origin as internal/component
extensions. When applying a backup (restore), the sync tool:

- Deletes `Default\Extensions\`
- Deletes `Default\Local Extension Settings\`
- Clears `extensions.settings` from `Default\Preferences`

Despite this, uBlock Origin reappears after the next Helium launch. Helium
re-injects its bundled extensions unconditionally — presumably from a manifest
inside its installation directory — so profile-level cleanup has no effect on them.

---

## 3. Default search engine is not removed on apply-backup

When applying a backup, the sync tool restores user-created search shortcuts
(`prepopulate_id = 0`) and updates `default_search_provider.guid` in Preferences.
However, Helium resets the default search engine to its own built-in default on
next launch.

Additionally, the backup's `search_shortcuts.json` may not have a matching entry
for the exact search engine Helium uses as its default (different `prepopulate_id`
or different URL template), so the round-trip fails silently.

**Result:** After applying a backup and launching Helium, the default search engine
reverts to whatever Helium considers its own default, regardless of what was
restored.

---

## Summary

| Feature                             | Status                             |
| ----------------------------------- | ---------------------------------- |
| Extension install via file stubs    | ✗ Not working                      |
| Extension install via registry      | ✗ Not working (tested)             |
| Built-in extension (uBlock) removal | ✗ Not possible — Helium re-injects |
| Default search engine restore       | ✗ Helium overrides on launch       |
| Bookmarks sync                      | ✓ Works                            |
| Custom Dictionary sync              | ✓ Works                            |
| Local Storage sync                  | ✓ Works                            |
| Preferences (partial) sync          | ✓ Works                            |

---

## Current webstore_extensions.json

Resolved from Thorium's `Extensions/` directory for comparison.

1. AHA Music - Song Finder for Browser — `dpacanjfikmhoddligfbehkpomnbgblf`
2. Augmented Steam — `dnhpnfgdlenaccegplpojghhmaamnnfp`
3. AutoScroll — `occjjkgifpmdgodlplnacmkejpdionan`
4. Claude — `fcoeoabgfenejglbffodgkkbkcdhcgfn`
5. Control Panel for Twitter — `kpmjjdhbcfebfjgdnpjagcndoelnidfj`
6. Dark Reader — `eimadpbcbfnmbkopoojfekhnkhdbieeh`
7. Disable Twitch extensions — `nmogopjdbhhnbkiklkdahphkdpbjfine`
8. Dracula Chrome Theme — `gfapcejdoghpoidkfodoiiffaaibpaem`
9. DynamicHistory — `ehkdegpnplleadlmjoaidmjiabocgpok`
10. Enhancer for YouTube™ — `ponfpcnoihfmfllpaingbgckeeldkhle`
11. KeePassXC-Browser — `oboonakemofpalcgghocfoadofidjkkk`
12. Keepa™ - Amazon Price Tracker — `neebplgakaahbhdphmkckjjcegoiijjo`
13. Location Flags for X (Twitter) — `jnpglhiolmmfchhpoipnknmffmpmogmc`
14. Reddit Untranslate — `eninkmbmgkpkcelmohdlgldafpkfpnaf`
15. Redirector — `lioaeidejmlpffbndjhaameocfldlhin`
16. Search by Image — `cnojnbdhbhnkbcieeekonklommdnndci`
17. Select like a Boss — `mbnnmpmcijodolgeejegcijdamonganh`
18. SponsorBlock for YouTube - Skip Sponsorships — `mnjggcdmjocbbbhaepdhchncahnbgone`
19. Stylus — `clngdbkpkpeebahjckkjfobafhncgmne`
20. uBlock Origin — `cjpalhdlnbpafiamejdnhcphjbkeiagm`
21. Violentmonkey — `jinjaccalgkegednnccohejagnlnfdag`
22. VK Next - functions for VK — `jephanpkonkmnkekmlkcijdjgniikppl`
23. YouTube Anti Translate — `ndpmhjnlfkgfalaieeneneenijondgag`

### To not forget to add later

24. Autofill for Code web chat
25. something else...
