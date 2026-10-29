from __future__ import annotations

import json
import logging
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import src.config as _config
from src.operations import locked
from src.sync import copy_file_atomic, write_text_if_changed
from src.sync import extensions as _extensions
from src.sync import flags as _flags
from src.sync import history as _history
from src.sync import leveldb as _leveldb
from src.sync import prefs as _prefs
from src.sync import shortcuts as _shortcuts
from src.sync import sync_dir as _sync_dir
from src.sync.extension_install import install_external_extensions

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

_LOG = logging.getLogger(__name__)

_is_empty = _leveldb.is_empty_leveldb


@dataclass(frozen=True)
class ProfileSync:
    browser: BrowserBase
    path: Path
    direction: str
    restore: bool
    pending: frozenset[str]
    extensions_only: bool = False


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
        copy_file_atomic(src, dst)
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

    @locked
    def sync_browser_profile(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        *,
        direction: str = "both",
        on_progress: Callable[[str], None] | None = None,
        ext_id_aliases: dict[str, str] | None = None,
        defer_ext_ids: set[str] | None = None,
    ) -> None:
        self._progress_cb = on_progress
        dt = data_types or {}
        sync_profile_path.mkdir(parents=True, exist_ok=True)
        _LOG.info("Syncing profile: %s ↔ %s", profile_path, sync_profile_path)

        if dt.get("extensions", True):
            skip = set(_config.get_excluded_ext_settings_ids())
            skip.update(_extensions._with_alias_ids(defer_ext_ids or set(), ext_id_aliases))
            if direction in ("push", "both"):
                _extensions.update_webstore_manifest(
                    profile_path,
                    sync_profile_path,
                    ext_id_aliases,
                    ignore_removals=defer_ext_ids,
                )
            keep_ext_ids = _extensions.extension_install_ids(
                sync_profile_path, profile_path, ext_id_aliases
            )
            pruned = _extensions.preserve_orphan_extension_data(
                sync_profile_path, keep_ext_ids, ext_id_aliases
            )
            if pruned:
                _LOG.info(
                    "Saved orphan extension data: %s",
                    ", ".join(sorted(pruned)),
                )

            self._sync_leveldb(
                profile_path,
                sync_profile_path,
                "Local Extension Settings",
                direction,
                name_filter=lambda n, s=skip, keep=keep_ext_ids: n in keep and n not in s,
            )
            self._sync_leveldb(
                profile_path,
                sync_profile_path,
                "Sync Extension Settings",
                direction,
                name_filter=lambda n, s=skip, keep=keep_ext_ids: n in keep and n not in s,
            )
            self._sync_leveldb(
                profile_path,
                sync_profile_path,
                "IndexedDB",
                direction,
                name_filter=lambda n, s=skip, keep=keep_ext_ids: (
                    (ext_id := _extensions.indexeddb_extension_id(n)) in keep and ext_id not in s
                ),
            )

        if dt.get("local_storage", True):
            self._sync_leveldb(profile_path, sync_profile_path, "Local Storage", direction)

        s, sk = _prefs.sync_preferences_json(
            profile_path,
            sync_profile_path,
            direction,
            self._report,
            include_extensions=dt.get("extensions", True),
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

        sync_root = sync_profile_path
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
                extract=lambda: _history.extract_typed_urls(profile_path, sync_root, self._report),
                restore=lambda: _history.restore_typed_urls(profile_path, sync_root, self._report),
                ts_key="last_visit_time",
            )

        _LOG.info("Profile sync complete: %s", profile_path.name)

    @locked
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
            raise FileNotFoundError(f"Backup does not exist: {sync_profile_path}")

        _LOG.info("Restoring profile from backup: %s → %s", sync_profile_path, profile_path)

        selected: list[Path] = []
        for filename, data_type in (
            ("Bookmarks", "bookmarks"),
            ("Custom Dictionary.txt", "custom_dictionary"),
            ("Shortcuts", "omnibox_shortcuts"),
            ("Local Storage", "local_storage"),
        ):
            if dt.get(data_type, True) and (sync_profile_path / filename).exists():
                selected.append(Path(filename))

        if dt.get("extensions", True):
            aliases = browser.ext_id_aliases if browser is not None else None
            keep = _extensions.extension_install_ids(sync_profile_path, profile_path, aliases)
            excluded = set(_config.get_excluded_ext_settings_ids())
            if browser is not None and not browser.ungoogled:
                excluded.update(_config.get_ungoogled_only_extensions())
            for parent in ("Local Extension Settings", "Sync Extension Settings", "IndexedDB"):
                base = sync_profile_path / parent
                if not base.is_dir():
                    continue
                for entry in base.iterdir():
                    ext_id = (
                        _extensions.indexeddb_extension_id(entry.name)
                        if parent == "IndexedDB"
                        else entry.name
                    )
                    if entry.is_dir() and ext_id in keep and ext_id not in excluded:
                        selected.append(entry.relative_to(sync_profile_path))

        # Finish copying before replacing any live data. A failed transfer must not
        # leave an otherwise healthy profile with its old databases already deleted.
        profile_path.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="cps-restore-", dir=profile_path.parent) as temp:
            staging = Path(temp)
            for relative in selected:
                source = sync_profile_path / relative
                destination = staging / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._report(relative.as_posix())
                if source.is_dir():
                    shutil.copytree(source, destination, ignore=shutil.ignore_patterns("._*"))
                else:
                    shutil.copy2(source, destination)
            for relative in selected:
                source, target = staging / relative, profile_path / relative
                if not source.exists():
                    continue
                if source.is_dir():
                    if _is_empty(source) and target.exists() and not _is_empty(target):
                        continue
                    _leveldb.copy_atomic(source, target, self._report)
                else:
                    source.replace(target)
                self._synced_count += 1

        json_path = sync_profile_path / "preferences.json"
        prefs_path = profile_path / "Preferences"
        if json_path.exists() and prefs_path.exists():
            saved = _prefs.selected_preferences(
                json.loads(json_path.read_bytes()), include_extensions=dt.get("extensions", True)
            )
            prefs = json.loads(prefs_path.read_bytes())
            _prefs.merge_prefs(prefs, saved)
            write_text_if_changed(prefs_path, json.dumps(prefs, separators=(",", ":")))
            _LOG.info("Merged preferences.json into %s", prefs_path)

        if dt.get("search_shortcuts", True):
            _shortcuts.restore_search_shortcuts(profile_path, sync_profile_path, self._report)

        if dt.get("typed_urls", True):
            _history.restore_typed_urls(profile_path, sync_profile_path, self._report)

        _LOG.info("Profile restore complete: %s", profile_path.name)

    def _translate_ext_aliases(
        self, work_dir: Path, aliases: dict[str, str], *, to_alias: bool
    ) -> None:
        for subdir in ("Local Extension Settings", "Sync Extension Settings", "IndexedDB"):
            base = work_dir / subdir
            if not base.exists():
                continue
            for alias_id, canonical_id in aliases.items():
                src_id = canonical_id if to_alias else alias_id
                dst_id = alias_id if to_alias else canonical_id
                sources = (
                    list(base.glob(f"chrome-extension_{src_id}_*"))
                    if subdir == ("IndexedDB")
                    else [base / src_id]
                )
                for src in sources:
                    if not src.exists():
                        continue
                    name = src.name.replace(src_id, dst_id, 1)
                    dst = base / name
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

    def _sync_browser_flags(
        self, browser: BrowserBase, sync_root: Path, direction: str = "both"
    ) -> None:
        local_state = browser.local_state_path()
        if local_state is None:
            return
        ignore = _config.get_flags_ignore()
        s, sk = _flags.sync_flags(browser.name, local_state, sync_root, ignore, direction=direction)
        self._synced_count += s
        self._skipped_count += sk

    def _repull_ext_data(
        self,
        profile_path: Path,
        source_dir: Path,
        aliases: dict[str, str] | None = None,
        ext_ids: set[str] | None = None,
    ) -> None:
        if not source_dir.exists():
            return
        skip = set(_config.get_excluded_ext_settings_ids())
        keep_ext_ids = _extensions.extension_install_ids(source_dir, profile_path, aliases)
        if ext_ids is not None:
            keep_ext_ids &= _extensions._with_alias_ids(ext_ids, aliases)
        pruned = _extensions.preserve_orphan_extension_data(source_dir, keep_ext_ids, aliases)
        if pruned:
            _LOG.info("Pruned orphan extension data before repull: %s", ", ".join(sorted(pruned)))
        for sub in ("Local Extension Settings", "Sync Extension Settings"):
            src_base = source_dir / sub
            if not src_base.exists():
                continue
            dst_base = profile_path / sub
            dst_base.mkdir(parents=True, exist_ok=True)
            for ext_dir in src_base.iterdir():
                if not ext_dir.is_dir() or ext_dir.name in skip or ext_dir.name not in keep_ext_ids:
                    continue
                dst = dst_base / ext_dir.name
                if _is_empty(ext_dir) and dst.exists() and not _is_empty(dst):
                    _LOG.info(
                        "Ext repull: kept profile %s/%s — source is empty stub",
                        sub,
                        ext_dir.name,
                    )
                    continue
                _leveldb.copy_atomic(ext_dir, dst, self._report)
                self._synced_count += 1
        src_idb = source_dir / "IndexedDB"
        if src_idb.exists():
            dst_idb = profile_path / "IndexedDB"
            dst_idb.mkdir(parents=True, exist_ok=True)
            for d in src_idb.iterdir():
                if not d.is_dir() or not d.name.startswith("chrome-extension_"):
                    continue
                ext_id = _extensions.indexeddb_extension_id(d.name)
                if ext_id not in keep_ext_ids or ext_id in skip:
                    continue
                dst = dst_idb / d.name
                if _is_empty(d) and dst.exists() and not _is_empty(dst):
                    _LOG.info(
                        "Ext repull: kept profile IndexedDB/%s — source is empty stub",
                        d.name,
                    )
                    continue
                _leveldb.copy_atomic(d, dst, self._report)
                self._synced_count += 1

    def _sync_single_profile(
        self,
        browser: BrowserBase,
        profile_path: Path,
        work_dir: Path,
        direction: str,
        needs_restore: bool,
        pending: frozenset[str],
        data_types: dict[str, bool],
        extensions_only: bool = False,
    ) -> set[str]:
        aliases = browser.ext_id_aliases
        restore_src = work_dir
        completed: set[str] = set()
        if aliases:
            self._translate_ext_aliases(restore_src, aliases, to_alias=True)
        try:
            if needs_restore:
                self.restore_profile_from_backup(
                    profile_path,
                    work_dir,
                    data_types,
                    browser=browser,
                    on_progress=self._progress_cb,
                )
            else:
                installed = {
                    aliases.get(eid, eid)
                    for eid in (_extensions.installed_extension_ids(profile_path))
                }
                archived = _extensions.archived_ids(work_dir)
                observed = _config.get_extension_observation(profile_path)
                newly_installed = installed - set(observed) if observed is not None else set()
                ready = (set(pending) | newly_installed) & installed - archived
                if ready:
                    self._repull_ext_data(profile_path, work_dir, aliases or None, ready)
                completed = ready | (set(pending) & archived)
                if not extensions_only:
                    self.sync_browser_profile(
                        profile_path,
                        work_dir,
                        data_types,
                        direction=direction,
                        on_progress=self._progress_cb,
                        ext_id_aliases=aliases or None,
                        defer_ext_ids=set(pending) - completed,
                    )
        finally:
            if aliases:
                self._translate_ext_aliases(restore_src, aliases, to_alias=False)
        return completed

    @locked
    def sync_all(
        self,
        only_browser: str | None = None,
        only_profile: str | None = None,
        force_direction: str | None = None,
        on_progress: Callable[[str], None] | None = None,
        *,
        automatic: bool = True,
    ) -> dict:
        return self._sync_all(
            only_browser, only_profile, force_direction, on_progress, automatic=automatic
        )

    def _sync_all(
        self,
        only_browser: str | None = None,
        only_profile: str | None = None,
        force_direction: str | None = None,
        on_progress: Callable[[str], None] | None = None,
        *,
        automatic: bool = True,
    ) -> dict:
        self._progress_cb = on_progress
        enabled_browsers = _config.get_enabled_browsers()
        enabled_profiles = _config.get_enabled_profiles()
        directions = _config.get_profile_directions()
        profiles_needing_restore = _config.get_profiles_needing_restore()
        pending_restores = _config.get_pending_extension_restores()
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
                only_browser,
                only_profile,
                force_direction,
            )

        browsers_to_sync = [
            b for b in self.browsers if only_browser is None or b.name == only_browser
        ]

        targets: list[ProfileSync] = []
        for browser in browsers_to_sync:
            if enabled_browsers.get(browser.name) is False or not browser.is_installed():
                continue
            if browser.is_running():
                skipped_running.append(browser.name)
                continue
            allowed = enabled_profiles.get(browser.name, [])
            manual_pull = force_direction == "pull" and only_profile is not None
            if not allowed and not manual_pull:
                continue
            for profile in browser.discover_profiles():
                if (only_profile and profile.name != only_profile) or (
                    not manual_pull and profile.name not in allowed
                ):
                    continue
                direction = force_direction or directions.get(browser.name, {}).get(
                    profile.name, "both"
                )
                restore = force_direction == "pull" or profile.name in profiles_needing_restore.get(
                    browser.name, []
                )
                if restore:
                    direction = "pull"
                pending = frozenset(pending_restores.get(browser.name, {}).get(profile.name, []))
                paused = (
                    automatic
                    and not restore
                    and not _config.is_profile_sync_enabled(browser.name, profile.name)
                )
                if paused and not pending:
                    continue
                targets.append(ProfileSync(browser, profile, direction, restore, pending, paused))
        if not targets:
            return {"is_first_sync": is_first_sync, "skipped_running": skipped_running}

        current_dir = self.sync_folder / _sync_dir.SYNC_DIR_NAME
        if any(target.restore for target in targets) and not current_dir.is_dir():
            raise FileNotFoundError(f"Backup does not exist: {current_dir}")
        work_dir = Path(tempfile.mkdtemp(prefix="cps-work-"))

        ungoogled_only_ext_ids = _config.get_ungoogled_only_extensions()
        windows_only_ext_ids = _config.get_windows_only_extensions()
        excluded_ext_ids = _config.get_excluded_ext_settings_ids()

        success = False
        completed: list[tuple[ProfileSync, set[str]]] = []
        registration_browsers = {target.browser.name: target.browser for target in targets}
        try:
            baseline = {}
            if current_dir.exists():
                self._report("Loading sync state...")
                baseline = _sync_dir.seed_work_dir(current_dir, work_dir)
            self._prune_excluded_from_work(work_dir, excluded_ext_ids)

            # Reconcile removals first so browser iteration order cannot copy an
            # archived extension's stale settings over its saved backup.
            for target in targets:
                if (
                    not target.restore
                    and not target.extensions_only
                    and target.direction in ("push", "both")
                ):
                    _extensions.update_webstore_manifest(
                        target.path,
                        work_dir,
                        target.browser.ext_id_aliases,
                        ignore_removals=set(target.pending),
                    )
            for target in targets:
                if target.browser.is_running():
                    raise OSError(f"{target.browser.name} started during sync. Close it and retry.")
                self._report(f"{target.browser.name}/{target.path.name}")
                complete = self._sync_single_profile(
                    target.browser,
                    target.path,
                    work_dir,
                    target.direction,
                    target.restore,
                    target.pending,
                    data_types,
                    target.extensions_only,
                )
                completed.append((target, complete))
            for browser in registration_browsers.values():
                if browser.is_running():
                    raise OSError(f"{browser.name} started during sync. Close it and retry.")
                directions_for_browser = {
                    target.direction
                    for target in targets
                    if target.browser.name == browser.name and not target.extensions_only
                }
                if directions_for_browser:
                    flag_direction = (
                        next(iter(directions_for_browser))
                        if len(directions_for_browser) == 1
                        else "both"
                    )
                    self._sync_browser_flags(browser, work_dir, flag_direction)
                install_external_extensions(
                    work_dir,
                    browser,
                    ungoogled_only_ext_ids=ungoogled_only_ext_ids,
                    windows_only_ext_ids=windows_only_ext_ids,
                )
            success = True
        finally:
            try:
                if success and any(work_dir.iterdir()):
                    self._report("Syncing to folder...")
                    _sync_dir.merge_to_sync_dir(work_dir, current_dir, baseline)
                    for target, restored_ids in completed:
                        _extensions.record_observation(target.path, target.browser.ext_id_aliases)
                        if target.restore:
                            _config.set_last_restored_browser(target.browser.name)
                            installed = _extensions.installed_extension_ids(target.path)
                            aliases = target.browser.ext_id_aliases
                            installed = {aliases.get(eid, eid) for eid in installed}
                            active = _extensions.extension_install_ids(work_dir)
                            _config.request_extension_restore(
                                target.browser.name, target.path.name, active - installed
                            )
                            _config.clear_restore_flag(target.browser.name, target.path.name)
                        elif restored_ids:
                            _config.complete_extension_restores(
                                target.browser.name, target.path.name, restored_ids
                            )
            finally:
                shutil.rmtree(work_dir)

        summary = (
            f"Synced: {self._synced_count} items, Skipped: {self._skipped_count} items (unchanged)"
        )
        if is_first_sync:
            _LOG.info("Initial sync complete — %s", summary)
        else:
            _LOG.info("Sync complete — %s", summary)

        return {"is_first_sync": is_first_sync, "skipped_running": skipped_running}
