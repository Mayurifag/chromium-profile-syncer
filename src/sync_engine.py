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
from src.sync import archive as _archive
from src.sync import extensions as _extensions
from src.sync import favicons as _favicons
from src.sync import leveldb as _leveldb
from src.sync import prefs as _prefs
from src.sync import shortcuts as _shortcuts

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
    "favicons": True,
    "omnibox_shortcuts": True,
}

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

    def sync_browser_profile(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        *,
        direction: str = "both",
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self._progress_cb = on_progress
        dt = data_types or {}
        sync_profile_path.mkdir(parents=True, exist_ok=True)
        _LOG.info("Syncing profile: %s ↔ %s", profile_path, sync_profile_path)

        if dt.get("extensions", True):
            excluded_ext_ids = set(_config.get_excluded_ext_settings_ids())
            if direction in ("push", "both"):
                _extensions.update_webstore_manifest(profile_path, sync_profile_path)

            s, sk = _leveldb.sync_dir(
                profile_path, sync_profile_path, "Local Extension Settings", direction,
                self._report,
                name_filter=lambda n, ex=excluded_ext_ids: n not in ex,
            )
            self._synced_count += s
            self._skipped_count += sk

            s, sk = _leveldb.sync_dir(
                profile_path, sync_profile_path, "Sync Extension Settings", direction,
                self._report,
            )
            self._synced_count += s
            self._skipped_count += sk

            s, sk = _leveldb.sync_dir(
                profile_path, sync_profile_path, "IndexedDB", direction, self._report,
                name_filter=lambda n, ex=excluded_ext_ids: (
                    n.startswith("chrome-extension_")
                    and not any(n.startswith(f"chrome-extension_{e}_") for e in ex)
                ),
            )
            self._synced_count += s
            self._skipped_count += sk

        if dt.get("local_storage", True):
            s, sk = _leveldb.sync_dir(
                profile_path, sync_profile_path, "Local Storage/leveldb", direction, self._report,
            )
            self._synced_count += s
            self._skipped_count += sk

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

        if dt.get("search_shortcuts", True):
            if direction == "push":
                _shortcuts.extract_search_shortcuts(profile_path, sync_profile_path, self._report)
            if direction in ("pull", "both"):
                _shortcuts.restore_search_shortcuts(profile_path, sync_profile_path, self._report)

        if dt.get("favicons", True):
            if direction in ("push", "both"):
                _favicons.extract_favicons(profile_path, sync_profile_path, self._report)
            elif direction == "pull":
                _favicons.restore_favicons(profile_path, sync_profile_path, self._report)

        _LOG.info("Profile sync complete: %s", profile_path.name)

    def restore_profile_from_backup(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        *,
        browser: object | None = None,
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

        is_ungoogled = getattr(browser, "ungoogled", True)
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

        _LOG.info("Profile restore complete: %s", profile_path.name)

    def _prune_excluded_from_work(self, work_dir: Path, excluded_ext_ids: list[str]) -> None:
        for ext_id in excluded_ext_ids:
            stale = work_dir / "Local Extension Settings" / ext_id
            if stale.exists():
                shutil.rmtree(stale)
                _LOG.info("Pruned excluded ext settings: %s", ext_id)
            indexed_db = work_dir / "IndexedDB"
            if indexed_db.exists():
                for entry in indexed_db.iterdir():
                    if entry.is_dir() and entry.name.startswith(f"chrome-extension_{ext_id}_"):
                        shutil.rmtree(entry)
                        _LOG.info("Pruned excluded ext IndexedDB: %s", entry.name)

    def _sync_single_profile(
        self,
        browser: BrowserBase,
        profile_path: Path,
        work_dir: Path,
        direction: str,
        needs_restore: bool,
        force_direction: str | None,
        data_types: dict[str, bool],
        ungoogled_only_ext_ids: list[str],
        ext_browser_restrictions: dict[str, list[str]],
    ) -> None:
        if needs_restore:
            self.restore_profile_from_backup(
                profile_path, work_dir, data_types,
                browser=browser, on_progress=self._progress_cb,
            )
            _extensions.install_external_extensions(
                work_dir, browser,
                ungoogled_only_ext_ids=ungoogled_only_ext_ids,
                browser_restrictions=ext_browser_restrictions,
            )
            if force_direction != "pull":
                _config.clear_restore_flag(browser.name, profile_path.name)
        else:
            self.sync_browser_profile(
                profile_path, work_dir, data_types,
                direction=direction, on_progress=self._progress_cb,
            )
            if direction in ("pull", "both"):
                _extensions.install_external_extensions(
                    work_dir, browser,
                    ungoogled_only_ext_ids=ungoogled_only_ext_ids,
                    browser_restrictions=ext_browser_restrictions,
                )

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
        data_types = DEFAULT_DATA_TYPES

        is_first_sync = not (self.sync_folder / "current.tar").exists()
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

        current_archive = self.sync_folder / "current.tar"
        work_dir = Path(tempfile.mkdtemp(prefix="cps-work-"))

        ungoogled_only_ext_ids = _config.get_ungoogled_only_extensions()
        excluded_ext_ids = _config.get_excluded_ext_settings_ids()
        ext_browser_restrictions = _config.get_extension_browser_restrictions()

        success = False
        try:
            if current_archive.exists():
                self._report("Unpacking...")
                _archive.unpack_archive(current_archive, work_dir)

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
                if not allowed:
                    _LOG.info(
                        "Browser %s: no enabled profiles in config — skipping", browser.name
                    )
                    continue

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
                    try:
                        self._sync_single_profile(
                            browser, profile_path, work_dir, direction, needs_restore,
                            force_direction, data_types, ungoogled_only_ext_ids,
                            ext_browser_restrictions,
                        )
                    except OSError:
                        _LOG.exception(
                            "Failed to sync profile %s for browser %s",
                            profile_path.name, browser.name,
                        )
                        if needs_restore and force_direction != "pull":
                            raise
            success = True
        finally:
            if success and any(work_dir.iterdir()):
                if _archive.validate_archive_content(work_dir):
                    self._report("Packing...")
                    _archive.pack_to_archive(work_dir, current_archive)
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
