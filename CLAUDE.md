# Project Conventions

## Code

- Never hand-edit `uv.lock`. Use `uv add` / `uv remove` for deps.
- No backward compat — project is author-only. Less code, brave refactors, keep clean.

## Running Commands

Always prefix with `uv run`:

~~~sh
uv run pytest
uv run ruff check src tests
uv run python -m src.main
uv run python build.py
~~~

## After Fixes

Reinstall and relaunch after code changes:

~~~sh
make install
~~~

Builds app, kills running instance, installs, launches.

## Technology Constraints

- **PySide6** — no PyQt6 or other bindings.
- **Python 3.12+** — no syntax/stdlib unavailable in 3.12.
- **rclone** — required runtime dep for sync progress (install via system package manager).

## Code Style

- Line length: 100 characters
- Ruff rules: E, F, I, UP
- Target: Python 3.12
- **No obvious comments** — only explain *why*, not *what*. Code self-documents via naming.

## Key Implementation Details

### Windows Registry — Extension Policy Keys

**Never wipe `HKCU\SOFTWARE\Policies\*` or `HKCU\SOFTWARE\Chromium\Extensions`
(or any shared Chromium registry key) outside of what `_install_extensions_via_registry`
and `_install_extensions_via_force_list` already manage.**

Keys shared across all Chromium-based browsers. Bulk-deleting (e.g. browser cleanup) silently breaks extension install for unrelated browsers reading same paths.

### Linux Extension Auto-Install — Force-List Policy + Bare IDs

**Force-list (`ExtensionInstallForcelist`) entries on Linux MUST be bare IDs, not `id;url` pairs.**

Path: `/etc/chromium/policies/managed/<browser>-syncer.json` (system-wide, requires `pkexec` to write — file 0644 root-owned but world-readable; dir 0755 root-owned). Helium reads `/etc/chromium/policies` (confirmed via `strings /opt/helium-browser-bin/helium`); other Chromium forks may read their own subtree (e.g. `/etc/thorium/policies`) — extend `linux_managed_policy_dir()` per-browser.

Schema:
~~~json
{ "ExtensionInstallForcelist": ["dpacanjfikmhoddligfbehkpomnbgblf", ...] }
~~~

**Why bare ID, not `id;url`:**
- Helium's own update server (`https://services.helium.imput.net/service/update2/crx`) returns **404** for arbitrary Web Store extensions — only serves Helium-bundled ones (uBlock under internal id `blockjmkbacgjkknlgpkjjiijinjdanf`, KeePassXC patches, etc).
- Hard-coding `clients2.google.com` in the URL also doesn't auto-install on Helium.
- Omitting the URL makes Chromium use its built-in default — intercepted by Helium's bundled extension `extstore-fixups` (id `jfnekidfgkhnfagaabpddmioknbjglgp`), which proxies Web Store fetches. This is the only reliable path.
- Per Chromium policy schema, `<id>` (bare) is valid alongside `<id>;<update_url>`. Use bare everywhere force-list is supported (Linux policy file + Windows `ExtensionInstallForcelist` registry).

**Per-extension stubs (`External Extensions/<id>.json` with `external_update_url`) are different:** they REQUIRE a URL. Default URL stays `clients2.google.com/service/update2/crx` (`BrowserBase.web_store_update_url`). Don't override per-browser unless the browser actually has a working alternate proxy that serves arbitrary Web Store IDs.

**Browser must be fully restarted** for new policy file to be picked up — Chromium reads policies at startup only, no live reload.

**Manifest preservation:** `update_webstore_manifest` must skip writing when profile has 0 web-store extensions in `Extensions/` AND existing manifest has entries — fresh ungoogled browsers (Helium) create empty `Extensions/` on first launch and would otherwise wipe the prior cross-browser sync state. See guard in `src/sync/extensions.py:update_webstore_manifest`.

### Search Shortcuts Sync

Extracts user-created search engines (`prepopulate_id = 0`) from `Web Data` SQLite, stores as `search_shortcuts.json` inside tar (at `work_dir` root).

**Windows-specific — url_hash mandatory:**
- Every keywords row needs valid 64-byte `url_hash` BLOB
- Formula: `b'v10' + nonce(12) + AES-256-GCM(key, nonce, b'\x01' + SHA256(Pickle(id, url))) + tag(16)`
- AES key from `Local State` → `os_crypt.encrypted_key` (strip 5-byte `DPAPI` prefix, DPAPI-decrypt)
- Rows with missing/invalid hash silently dropped at Chromium startup
- See `docs/search-shortcuts.md` for full formula and Python implementation

**sync_guid rules:**
- Default engine: `sync_guid` must match `Preferences["default_search_provider"]["guid"]`
- All others: `sync_guid = ""` (local-only; Chrome deletes unknown UUIDs on reconciliation)

**Restore scope:** `DELETE FROM keywords` — wipe all engines (built-ins included), reinsert only synced shortcuts starting at ID 1. Then bump `meta.'Builtin Keyword Version'` to `99999` — without this, Chromium/Helium detects missing built-ins on startup and repopulates them (Helium uses `meta` table, not `keywords_metadata`). Also in same transaction: `UPDATE keywords_metadata SET value = <new_id> WHERE key = 'Default Search Provider ID'` and `DELETE FROM keywords_metadata WHERE key = 'Default Search Provider Backup'` — stale entries here override restored default (Backup blob silently rewrites `Preferences.default_search_provider.guid` on startup). Wrap in `try/except OperationalError` for older browsers.

**Choice screen:** Helium (and Chromium 127+) enables a search-engine choice screen. Until all three completion keys are present in Preferences, `GetChoiceCompletionMetadata` returns an error and the service wipes the record, ignoring `default_search_provider.guid`:
- `default_search_provider.choice_screen_completion_timestamp` — **JSON string** (not int!), seconds since Windows epoch (1601-01-01); integer type silently fails validation
- `default_search_provider.choice_screen_completion_version` — String, browser version (e.g. `"146.0.7680.177"`), must have same component count as running binary
- `default_search_provider.choice_screen_completion_program` — Int, `Program::kWaffle = 3` for Helium (all regions forced to kWaffle by its patch)
- `default_search_provider.reset_occurred` — must be `false`; absence triggers reset on launch

Only write these when `choice_screen_random_shuffle_seed` is already present (signals choice screen is active). Read version from `User Data/Last Version`. None of these keys are MAC-protected.

**`default_search_provider_data.mirrored_template_url_data`:** `DefaultSearchManager` uses this as the authoritative DSE cache. Must be written on restore with Helium's full schema — key differences from minimal builds: `input_encodings` is an array (not string), `id`/`date_created`/`last_modified`/`last_visited` are strings, `synced_guid` (not `sync_guid`), `suggestions_url` (not `suggest_url`), plus extra fields: `doodle_url`, `preconnect_to_search_url`, `prefetch_likely_navigations`, `image_translate_url`, `policy_origin`, `originating_url`, `usage_count`, `search_intent_params`, `contextual_search_url`, `logo_url`. See `_build_mirror_dict` in `src/sync/shortcuts.py`.
