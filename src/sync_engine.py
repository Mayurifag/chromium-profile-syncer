from __future__ import annotations

import json
import logging
import shutil
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import src.config as _config
from src import rclone as _rclone
from src.sync import extensions as _extensions
from src.sync import flags as _flags
from src.sync import history as _history
from src.sync import leveldb as _leveldb
from src.sync import prefs as _prefs
from src.sync import shortcuts as _shortcuts
from src.sync import sync_dir as _sync_dir
from src.sync import write_text_if_changed

if TYPE_CHECKING:
    from src.browsers.base import BrowserBase

NEVER_SYNC: frozenset[str] = frozenset(
    ["Login Data", "Cookies", "Web Data", "History", "Secure Preferences"]
)

DEFAULT_DATA_TYPES: dict[str, bool] = {
    "extensions": True,
    "bookmarks": True,
    "custom_dictionary": True,
    "local_storage": True,
    "search_shortcuts": True,
    "omnibox_shortcuts": True,
    "typed_urls": True,
}

_UBLOCK_CACHE_SKIP_PREFIXES: tuple[bytes, ...] = (b"cache/", b"compiled/", b"selfie/")
_LDB_CACHE_SKIP_PREFIXES: dict[str, tuple[bytes, ...]] = {
    "cjpalhdlnbpafiamejdnhcphjbkeiagm": _UBLOCK_CACHE_SKIP_PREFIXES,
    "blockjmkbacgjkknlgpkjjiijinjdanf": _UBLOCK_CACHE_SKIP_PREFIXES,
}
_IDB_FILTER_CACHE_ONLY_EXT_IDS: frozenset[str] = frozenset({
    "blockjmkbacgjkknlgpkjjiijinjdanf",
})

_LOG = logging.getLogger(__name__)


class SyncEngine:
    def __init__(
        self,
        sync_folder: Path,
        browsers: list[BrowserBase] | None = None,
    ) -> None:
        self.sync_folder = sync_folder
        if browsers is None:
            from src.browsers import ALL_BROWSERS
            browsers = ALL_BROWSERS
        self.browsers = browsers
        self._progress_cb: Callable[[str], None] | None = None
        self._synced_count: int = 0
        self._skipped_count: int = 0

    def _report(self, description: str) -> None:
        if self._progress_cb:
            self._progress_cb(description)

    def _copy(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        self._report(src.name)
        _LOG.info("Copying file: %s → %s", src.name, dst)
        shutil.copy2(src, dst)
        self._synced_count += 1

    def _sync_leveldb(
        self, profile_dir: Path, sync_path: Path, subpath: str, direction: str, **kwargs
    ) -> None:
        s, sk = _leveldb.sync_dir(
            profile_dir, sync_path, subpath, direction, self._report, **kwargs
        )
        self._synced_count += s
        self._skipped_count += sk

    def _sync_file(self, src: Path, dst: Path, direction: str = "both") -> None:
        src_mtime = src.stat().st_mtime if src.exists() else 0.0
        dst_mtime = dst.stat().st_mtime if dst.exists() else 0.0
        if src_mtime == dst_mtime:
            self._skipped_count += 1
            return
        if direction == "push":
            if src_mtime > dst_mtime:
                self._copy(src, dst)
            else:
                self._skipped_count += 1
        elif direction == "pull":
            if dst_mtime > src_mtime:
                self._copy(dst, src)
            else:
                self._skipped_count += 1
        else:
            self._copy(src, dst) if src_mtime > dst_mtime else self._copy(dst, src)

    def _sync_root_json(
        self,
        direction: str,
        *,
        remote_json: Path,
        snapshot: Callable[[], list[dict] | None],
        extract: Callable[[], None],
        restore: Callable[[], None],
        ts_key: str,
    ) -> None:
        if direction == "push":
            extract()
            return
        if direction == "pull":
            restore()
            return

        local = snapshot()
        if local is None:
            if remote_json.exists():
                restore()
            return
        if not remote_json.exists():
            extract()
            return

        try:
            remote = json.loads(remote_json.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            extract()
            return

        if local == remote:
            return

        local_ts = max((e.get(ts_key, 0) or 0 for e in local), default=0)
        remote_ts = max((e.get(ts_key, 0) or 0 for e in remote), default=0)
        if local_ts > remote_ts:
            extract()
        else:
            restore()

    def sync_browser_profile(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        *,
        direction: str = "both",
        on_progress: Callable[[str], None] | None = None,
        ext_id_aliases: dict[str, str] | None = None,
    ) -> None:
        self._progress_cb = on_progress
        dt = data_types or {}
        sync_profile_path.mkdir(parents=True, exist_ok=True)
        _LOG.info("Syncing profile: %s ↔ %s", profile_path, sync_profile_path)

        if dt.get("extensions", True):
            skip = set(_config.get_excluded_ext_settings_ids())
            if direction in ("push", "both"):
                _extensions.update_webstore_manifest(
                    profile_path, sync_profile_path, ext_id_aliases
                )

            les_key_filter = {
                k: v for k, v in _LDB_CACHE_SKIP_PREFIXES.items() if k not in skip
            }
            self._sync_leveldb(
                profile_path, sync_profile_path, "Local Extension Settings", direction,
                name_filter=lambda n, s=skip: n not in s,
                key_skip_prefixes=les_key_filter or None,
            )
            self._sync_leveldb(
                profile_path, sync_profile_path, "Sync Extension Settings", direction
            )
            idb_skip = skip | _IDB_FILTER_CACHE_ONLY_EXT_IDS
            self._sync_leveldb(
                profile_path, sync_profile_path, "IndexedDB", direction,
                name_filter=lambda n, s=idb_skip: (
                    n.startswith("chrome-extension_")
                    and not any(n.startswith(f"chrome-extension_{e}_") for e in s)
                ),
            )

        if dt.get("local_storage", True):
            self._sync_leveldb(profile_path, sync_profile_path, "Local Storage", direction)

        s, sk = _prefs.sync_preferences_json(
            profile_path, sync_profile_path, direction, self._report
        )
        self._synced_count += s
        self._skipped_count += sk

        plain_files: list[tuple[str, str | None]] = [
            ("Bookmarks", "bookmarks"),
            ("Custom Dictionary.txt", "custom_dictionary"),
            ("Shortcuts", "omnibox_shortcuts"),
        ]
        for filename, key in plain_files:
            if key is not None and not dt.get(key, True):
                continue
            src = profile_path / filename
            dst = sync_profile_path / filename
            if src.exists() or dst.exists():
                self._sync_file(src, dst, direction)

        # Root-level JSONs bypass work_dir so receivers can see prior pushes.
        sync_root = self.sync_folder / _sync_dir.SYNC_DIR_NAME
        sync_root.mkdir(parents=True, exist_ok=True)

        if dt.get("search_shortcuts", True):
            self._sync_root_json(
                direction,
                remote_json=sync_root / "search_shortcuts.json",
                snapshot=lambda: _shortcuts.snapshot_shortcuts(profile_path),
                extract=lambda: _shortcuts.extract_search_shortcuts(
                    profile_path, sync_root, self._report
                ),
                restore=lambda: _shortcuts.restore_search_shortcuts(
                    profile_path, sync_root, self._report
                ),
                ts_key="last_modified",
            )

        if dt.get("typed_urls", True):
            self._sync_root_json(
                direction,
                remote_json=sync_root / "typed_urls.json",
                snapshot=lambda: _history.snapshot_typed_urls(profile_path),
                extract=lambda: _history.extract_typed_urls(
                    profile_path, sync_root, self._report
                ),
                restore=lambda: _history.restore_typed_urls(
                    profile_path, sync_root, self._report
                ),
                ts_key="last_visit_time",
            )

        _LOG.info("Profile sync complete: %s", profile_path.name)

    def restore_profile_from_backup(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        *,
        browser: BrowserBase | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self._progress_cb = on_progress
        dt = data_types or {}

        if not sync_profile_path.exists():
            _LOG.warning("Backup does not exist at %s — cannot restore", sync_profile_path)
            return

        _LOG.info("Restoring profile from backup: %s → %s", sync_profile_path, profile_path)

        _ext_parents = {"Local Extension Settings", "Sync Extension Settings", "IndexedDB"}
        for src in sync_profile_path.iterdir():
            dst = profile_path / src.name
            if not dst.exists():
                continue
            if src.name in _ext_parents and src.is_dir() and dst.is_dir():
                for ext_sub in src.iterdir():
                    ext_dst = dst / ext_sub.name
                    if ext_dst.exists():
                        shutil.rmtree(ext_dst)
                        self._synced_count += 1
            elif dst.is_dir():
                _LOG.debug("Deleting directory: %s", dst)
                shutil.rmtree(dst)
                self._synced_count += 1
            else:
                _LOG.debug("Deleting file: %s", dst)
                dst.unlink()
                self._synced_count += 1

        is_ungoogled = browser.ungoogled if browser is not None else True
        excluded_ext_ids: list[str] = (
            [] if is_ungoogled else _config.get_ungoogled_only_extensions()
        )

        cmd = [
            str(_rclone.find_rclone() or "rclone"), "copy",
            str(sync_profile_path), str(profile_path),
            "--stats", "1s",
            "--stats-one-line",
            "--transfers", "8",
            "--checkers", "16",
            "--exclude", "._*",
            "--exclude", "preferences.json",
            "--exclude", "Extensions/**",
        ]
        for ext_id in excluded_ext_ids:
            cmd += ["--exclude", f"Extensions/{ext_id}/**"]
            cmd += ["--exclude", f"Local Extension Settings/{ext_id}/**"]
            cmd += ["--exclude", f"IndexedDB/chrome-extension_{ext_id}_*/**"]

        _rclone.run(cmd, "Restoring from backup", self._report)

        json_path = sync_profile_path / "preferences.json"
        prefs_path = profile_path / "Preferences"
        if json_path.exists() and prefs_path.exists():
            saved = json.loads(json_path.read_bytes())
            prefs = json.loads(prefs_path.read_bytes())
            if dt.get("extensions", True):
                prefs.get("extensions", {}).pop("settings", None)
                saved.get("extensions", {}).pop("settings", None)
            _prefs.merge_prefs(prefs, saved)
            prefs_path.write_bytes(json.dumps(prefs, separators=(",", ":")).encode())
            _LOG.info("Merged preferences.json into %s", prefs_path)

        if dt.get("search_shortcuts", True):
            _shortcuts.restore_search_shortcuts(profile_path, sync_profile_path, self._report)

        if dt.get("typed_urls", True):
            _history.restore_typed_urls(profile_path, sync_profile_path, self._report)

        _LOG.info("Profile restore complete: %s", profile_path.name)

    def restore_from_sync_folder(
        self,
        browsers: list[BrowserBase],
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        current_dir = self.sync_folder / _sync_dir.SYNC_DIR_NAME
        ungoogled_only = _config.get_ungoogled_only_extensions()
        windows_only = _config.get_windows_only_extensions()
        for b in browsers:
            if not b.is_installed():
                _LOG.info("%s: not installed — skipping", b.name)
                continue
            profiles = b.discover_profiles()
            if not profiles:
                _LOG.info("%s: no profiles found — skipping", b.name)
                continue
            for profile_path in profiles:
                self.restore_profile_from_backup(
                    profile_path, current_dir, browser=b, on_progress=on_progress,
                )
                _extensions.install_external_extensions(
                    current_dir, b,
                    ungoogled_only_ext_ids=ungoogled_only,
                    windows_only_ext_ids=windows_only,
                )

    def _translate_ext_aliases(
        self, work_dir: Path, aliases: dict[str, str], *, to_alias: bool
    ) -> None:
        for subdir in ("Local Extension Settings", "Sync Extension Settings"):
            base = work_dir / subdir
            if not base.exists():
                continue
            for alias_id, canonical_id in aliases.items():
                src_name = canonical_id if to_alias else alias_id
                dst_name = alias_id if to_alias else canonical_id
                src, dst = base / src_name, base / dst_name
                if not src.exists():
                    continue
                if dst.exists():
                    if _leveldb.dir_mtime(src) > _leveldb.dir_mtime(dst):
                        shutil.rmtree(dst)
                        src.rename(dst)
                    else:
                        shutil.rmtree(src)
                else:
                    src.rename(dst)

        prefs_json = work_dir / "preferences.json"
        if prefs_json.exists():
            try:
                prefs = json.loads(prefs_json.read_text(encoding="utf-8"))
                pinned = prefs.get("extensions", {}).get("pinned_extensions")
                if isinstance(pinned, list):
                    id_map = (
                        {canonical: alias for alias, canonical in aliases.items()}
                        if to_alias
                        else aliases
                    )
                    new_pinned = [id_map.get(eid, eid) for eid in pinned]
                    if new_pinned != pinned:
                        prefs.setdefault("extensions", {})["pinned_extensions"] = new_pinned
                        prefs_json.write_text(json.dumps(prefs), encoding="utf-8")
            except (OSError, json.JSONDecodeError):
                pass

    def _prune_excluded_from_work(self, work_dir: Path, excluded_ext_ids: list[str]) -> None:
        if not excluded_ext_ids:
            return
        les_dir = work_dir / "Local Extension Settings"
        for ext_id in excluded_ext_ids:
            stale = les_dir / ext_id
            if stale.exists():
                shutil.rmtree(stale)
                _LOG.info("Pruned excluded ext settings: %s", ext_id)
        indexed_db = work_dir / "IndexedDB"
        if indexed_db.exists():
            prefixes = tuple(f"chrome-extension_{e}_" for e in excluded_ext_ids)
            for entry in indexed_db.iterdir():
                if entry.is_dir() and entry.name.startswith(prefixes):
                    shutil.rmtree(entry)
                    _LOG.info("Pruned excluded ext IndexedDB: %s", entry.name)

    def _sync_browser_flags(self, browser: BrowserBase, sync_root: Path) -> None:
        local_state = browser.local_state_path()
        if local_state is None:
            return
        ignore = _config.get_flags_ignore()
        try:
            s, sk = _flags.sync_flags(browser.name, local_state, sync_root, ignore)
            self._synced_count += s
            self._skipped_count += sk
        except OSError:
            _LOG.exception("Flags sync failed for %s", browser.name)

    def _repull_ext_data(self, profile_path: Path, source_dir: Path) -> None:
        if not source_dir.exists():
            return
        skip = set(_config.get_excluded_ext_settings_ids())
        for sub in ("Local Extension Settings", "Sync Extension Settings"):
            src_base = source_dir / sub
            if not src_base.exists():
                continue
            dst_base = profile_path / sub
            dst_base.mkdir(parents=True, exist_ok=True)
            for ext_dir in src_base.iterdir():
                if not ext_dir.is_dir() or ext_dir.name in skip:
                    continue
                dst = dst_base / ext_dir.name
                prefixes = (
                    _LDB_CACHE_SKIP_PREFIXES.get(ext_dir.name)
                    if sub == "Local Extension Settings"
                    else None
                )
                if prefixes:
                    from src.sync.ldb_filter import copy_filtered
                    if dst.exists():
                        shutil.rmtree(dst)
                    copy_filtered(ext_dir, dst, prefixes)
                else:
                    _leveldb.copy_atomic(ext_dir, dst, self._report)
                self._synced_count += 1
        src_idb = source_dir / "IndexedDB"
        if src_idb.exists():
            dst_idb = profile_path / "IndexedDB"
            dst_idb.mkdir(parents=True, exist_ok=True)
            idb_skip = skip | _IDB_FILTER_CACHE_ONLY_EXT_IDS
            for d in src_idb.iterdir():
                if not d.is_dir() or not d.name.startswith("chrome-extension_"):
                    continue
                if any(d.name.startswith(f"chrome-extension_{e}_") for e in idb_skip):
                    continue
                _leveldb.copy_atomic(d, dst_idb / d.name, self._report)
                self._synced_count += 1

    def _sync_single_profile(
        self,
        browser: BrowserBase,
        profile_path: Path,
        work_dir: Path,
        direction: str,
        needs_restore: bool,
        needs_ext_repull: bool,
        force_direction: str | None,
        data_types: dict[str, bool],
        ungoogled_only_ext_ids: list[str],
        windows_only_ext_ids: list[str],
    ) -> None:
        aliases = browser.ext_id_aliases
        current_dir = self.sync_folder / _sync_dir.SYNC_DIR_NAME
        restore_src = current_dir if needs_restore else work_dir
        if aliases:
            self._translate_ext_aliases(restore_src, aliases, to_alias=True)
        try:
            if needs_restore:
                self.restore_profile_from_backup(
                    profile_path, current_dir, data_types,
                    browser=browser, on_progress=self._progress_cb,
                )
                _extensions.install_external_extensions(
                    current_dir, browser,
                    ungoogled_only_ext_ids=ungoogled_only_ext_ids,
                    windows_only_ext_ids=windows_only_ext_ids,
                )
                _config.set_last_restored_browser(browser.name)
                _config.mark_profile_for_ext_repull(browser.name, profile_path.name)
                _config.clear_restore_flag(browser.name, profile_path.name)
            else:
                if needs_ext_repull and direction in ("pull", "both"):
                    self._repull_ext_data(profile_path, work_dir)
                    _config.clear_ext_repull_flag(browser.name, profile_path.name)
                self.sync_browser_profile(
                    profile_path, work_dir, data_types,
                    direction=direction, on_progress=self._progress_cb,
                    ext_id_aliases=aliases or None,
                )
                if direction in ("pull", "both"):
                    _extensions.install_external_extensions(
                        work_dir, browser,
                        ungoogled_only_ext_ids=ungoogled_only_ext_ids,
                        windows_only_ext_ids=windows_only_ext_ids,
                    )
        finally:
            if aliases:
                self._translate_ext_aliases(restore_src, aliases, to_alias=False)

    def sync_all(
        self,
        only_browser: str | None = None,
        only_profile: str | None = None,
        force_direction: str | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> dict[str, bool]:
        self._progress_cb = on_progress
        enabled_browsers = _config.get_enabled_browsers()
        enabled_profiles = _config.get_enabled_profiles()
        directions = _config.get_profile_directions()
        profiles_needing_restore = _config.get_profiles_needing_restore()
        profiles_needing_ext_repull = _config.get_profiles_needing_ext_repull()
        data_types = DEFAULT_DATA_TYPES

        is_first_sync = not (self.sync_folder / _sync_dir.SYNC_DIR_NAME / "metadata.json").exists()
        self._synced_count = 0
        self._skipped_count = 0
        skipped_running: list[str] = []

        if is_first_sync:
            _LOG.info("Starting initial sync (first-time setup)")
        else:
            _LOG.info(
                "Starting sync_all (only_browser=%s, only_profile=%s, force_direction=%s)",
                only_browser, only_profile, force_direction,
            )

        browsers_to_sync = [
            b for b in self.browsers
            if only_browser is None or b.name == only_browser
        ]

        manageable = [
            b for b in browsers_to_sync
            if enabled_browsers.get(b.name) is not False and b.is_installed()
        ]
        if manageable and all(b.is_running() for b in manageable):
            running_names = [b.name for b in manageable]
            _LOG.warning(
                "All targeted browsers running (%s) — skipping sync",
                ", ".join(running_names),
            )
            return {"is_first_sync": is_first_sync, "skipped_running": running_names}

        current_dir = self.sync_folder / _sync_dir.SYNC_DIR_NAME
        work_dir = Path(tempfile.mkdtemp(prefix="cps-work-"))

        ungoogled_only_ext_ids = _config.get_ungoogled_only_extensions()
        windows_only_ext_ids = _config.get_windows_only_extensions()
        excluded_ext_ids = _config.get_excluded_ext_settings_ids()

        success = False
        try:
            if current_dir.exists():
                self._report("Loading sync state...")
                _sync_dir.seed_work_dir(current_dir, work_dir)
            self._prune_excluded_from_work(work_dir, excluded_ext_ids)

            for browser in browsers_to_sync:
                if enabled_browsers.get(browser.name) is False:
                    _LOG.debug("Browser %s disabled in settings — skipping", browser.name)
                    continue
                if not browser.is_installed():
                    _LOG.debug("Browser %s not installed — skipping", browser.name)
                    continue
                if browser.is_running():
                    _LOG.warning(
                        "Browser %s is running — skipping to avoid data corruption", browser.name,
                    )
                    skipped_running.append(browser.name)
                    continue

                profiles = browser.discover_profiles()
                if not profiles:
                    _LOG.info("Browser %s: no profiles found", browser.name)
                    continue

                allowed = enabled_profiles.get(browser.name)
                manual_pull = force_direction == "pull" and only_profile is not None
                if not allowed and not manual_pull:
                    _LOG.info(
                        "Browser %s: no enabled profiles in config — skipping", browser.name
                    )
                    continue

                if not manual_pull:
                    profiles = [p for p in profiles if p.name in allowed]
                if only_profile:
                    profiles = [p for p in profiles if p.name == only_profile]
                if not profiles:
                    _LOG.info(
                        "Browser %s: no matching profiles — skipping", browser.name
                    )
                    continue

                _LOG.info("Browser %s: syncing %d profile(s)", browser.name, len(profiles))
                for profile_path in profiles:
                    self._report(f"{browser.name}/{profile_path.name}")
                    direction = force_direction or directions.get(browser.name, {}).get(
                        profile_path.name, "both"
                    )
                    needs_restore = force_direction == "pull" or (
                        browser.name in profiles_needing_restore
                        and profile_path.name in profiles_needing_restore[browser.name]
                    )
                    needs_ext_repull = (
                        not needs_restore
                        and profile_path.name
                        in profiles_needing_ext_repull.get(browser.name, [])
                    )
                    try:
                        self._sync_single_profile(
                            browser, profile_path, work_dir, direction, needs_restore,
                            needs_ext_repull, force_direction, data_types,
                            ungoogled_only_ext_ids, windows_only_ext_ids,
                        )
                    except OSError:
                        _LOG.exception(
                            "Failed to sync profile %s for browser %s",
                            profile_path.name, browser.name,
                        )
                        if needs_restore and force_direction != "pull":
                            raise
                self._sync_browser_flags(browser, current_dir)
            keep_browsers = {bn for bn, profs in enabled_profiles.items() if profs}
            pruned = _flags.prune_sync_flags(current_dir, keep_browsers)
            if pruned:
                _LOG.info("Pruned flags for unused browser(s): %s", ", ".join(pruned))
            success = True
        finally:
            if success and any(work_dir.iterdir()):
                self._report("Syncing to folder...")
                _sync_dir.merge_to_sync_dir(work_dir, current_dir)
                write_text_if_changed(current_dir / "metadata.json", "{}")
            shutil.rmtree(work_dir)

        summary = (
            f"Synced: {self._synced_count} items, "
            f"Skipped: {self._skipped_count} items (unchanged)"
        )
        if is_first_sync:
            _LOG.info("Initial sync complete — %s", summary)
        else:
            _LOG.info("Sync complete — %s", summary)

        return {"is_first_sync": is_first_sync, "skipped_running": skipped_running}
