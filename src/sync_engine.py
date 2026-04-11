from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.browsers.base import BrowserBase

NEVER_SYNC: frozenset[str] = frozenset(
    ["Login Data", "Cookies", "Web Data", "History", "Secure Preferences"]
)

_LOG = logging.getLogger(__name__)


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a dotted version string into a comparable tuple.

    Returns (0,) on any parse failure.
    """
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


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
        self.logger = logging.getLogger(f"{__name__}.SyncEngine")

    # ------------------------------------------------------------------
    # Low-level primitives
    # ------------------------------------------------------------------

    def _copy_leveldb_atomic(self, src: Path, dst: Path) -> None:
        """Copy LevelDB directory src → dst atomically via a .tmp staging area.

        If the copy fails, dst is left untouched.  The .tmp dir may be left
        behind for manual cleanup but will never clobber dst.
        """
        tmp = dst.with_suffix(".tmp")
        try:
            if tmp.exists():
                shutil.rmtree(tmp)
            shutil.copytree(src, tmp)
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(tmp), dst)
            self.logger.debug("Atomic copy: %s → %s", src, dst)
        except OSError:
            self.logger.exception("Atomic copy failed: %s → %s (dst untouched)", src, dst)
            # dst is intentionally not touched; tmp may remain

    def _dir_mtime(self, directory: Path) -> float:
        """Return the maximum mtime of all files under *directory*.

        Returns 0.0 if the directory is empty or does not exist.
        """
        try:
            mtimes = [f.stat().st_mtime for f in directory.rglob("*") if f.is_file()]
        except OSError:
            return 0.0
        return max(mtimes) if mtimes else 0.0

    def _sync_file(self, src: Path, dst: Path, direction: str = "both") -> None:
        """Copy src/dst according to direction. Skips if mtimes are equal.

        direction: "both" = newest wins, "push" = src→dst only, "pull" = dst→src only.
        Creates parent directories as needed.
        """
        src_mtime = src.stat().st_mtime if src.exists() else 0.0
        dst_mtime = dst.stat().st_mtime if dst.exists() else 0.0
        if src_mtime == dst_mtime:
            return
        if direction == "push":
            if src_mtime > dst_mtime and src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                self.logger.info("Synced %s → %s", src, dst)
        elif direction == "pull":
            if dst_mtime > src_mtime and dst.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, src)
                self.logger.info("Synced %s → %s", dst, src)
        else:  # both
            if src_mtime > dst_mtime:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                self.logger.info("Synced %s → %s", src, dst)
            else:
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, src)
                self.logger.info("Synced %s → %s", dst, src)

    # ------------------------------------------------------------------
    # Extension sync (version-based)
    # ------------------------------------------------------------------

    def _sync_extensions(self, profile_dir: Path, sync_dir: Path, direction: str = "both") -> None:
        """Sync Extensions/ — higher manifest.json version wins."""
        profile_ext_dir = profile_dir / "Extensions"
        sync_ext_dir = sync_dir / "Extensions"

        # Collect all extension IDs from both sides
        ext_ids: set[str] = set()
        if profile_ext_dir.exists():
            ext_ids.update(d.name for d in profile_ext_dir.iterdir() if d.is_dir())
        if sync_ext_dir.exists():
            ext_ids.update(d.name for d in sync_ext_dir.iterdir() if d.is_dir())

        for ext_id in ext_ids:
            profile_id_dir = profile_ext_dir / ext_id
            sync_id_dir = sync_ext_dir / ext_id
            self._sync_extension_id(profile_id_dir, sync_id_dir, ext_id, direction)

    def _sync_extension_id(
        self, profile_id_dir: Path, sync_id_dir: Path, ext_id: str, direction: str = "both"
    ) -> None:
        """Sync one extension ID directory by picking the higher version."""
        # Find best version dir on each side
        profile_best = self._best_extension_version_dir(profile_id_dir)
        sync_best = self._best_extension_version_dir(sync_id_dir)

        if profile_best is None and sync_best is None:
            return

        profile_ver = self._extension_dir_version(profile_best) if profile_best else (0,)
        sync_ver = self._extension_dir_version(sync_best) if sync_best else (0,)

        if profile_ver == sync_ver:
            return  # already in sync

        if profile_ver > sync_ver:
            if direction in ("push", "both") and profile_best is not None:
                dest = sync_id_dir / profile_best.name
                self.logger.info(
                    "Extension %s: profile version %s > sync %s — copying to sync",
                    ext_id,
                    profile_ver,
                    sync_ver,
                )
                sync_id_dir.mkdir(parents=True, exist_ok=True)
                self._copy_leveldb_atomic(profile_best, dest)
        else:
            if direction in ("pull", "both") and sync_best is not None:
                dest = profile_id_dir / sync_best.name
                self.logger.info(
                    "Extension %s: sync version %s > profile %s — copying to profile",
                    ext_id,
                    sync_ver,
                    profile_ver,
                )
                profile_id_dir.mkdir(parents=True, exist_ok=True)
                self._copy_leveldb_atomic(sync_best, dest)

    def _best_extension_version_dir(self, id_dir: Path) -> Path | None:
        """Return the subdirectory with the highest parsed version, or None."""
        if not id_dir.exists():
            return None
        candidates: list[tuple[tuple[int, ...], Path]] = []
        for d in id_dir.iterdir():
            if d.is_dir():
                candidates.append((self._extension_dir_version(d), d))
        if not candidates:
            return None
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _extension_dir_version(self, version_dir: Path) -> tuple[int, ...]:
        """Extract version from an extension version directory.

        Tries manifest.json first; falls back to directory name (stripping _0 suffix).
        """
        manifest = version_dir / "manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                ver_str = data.get("version", "")
                if ver_str:
                    parsed = _parse_version(str(ver_str))
                    if parsed != (0,) or ver_str == "0":
                        return parsed
            except (OSError, json.JSONDecodeError, ValueError):
                pass
        # Fall back to directory name (strip _0 Chromium artifact suffix)
        raw_name = version_dir.name.split("_")[0]
        return _parse_version(raw_name)

    # ------------------------------------------------------------------
    # LevelDB sync (mtime-based)
    # ------------------------------------------------------------------

    def _sync_leveldb_dir(
        self, profile_dir: Path, sync_dir: Path, subpath: str, direction: str = "both"
    ) -> None:
        """For each LevelDB unit under *subpath*, copy according to direction atomically."""
        profile_base = profile_dir / subpath
        sync_base = sync_dir / subpath

        unit_names: set[str] = set()
        if profile_base.exists():
            unit_names.update(d.name for d in profile_base.iterdir() if d.is_dir())
        if sync_base.exists():
            unit_names.update(d.name for d in sync_base.iterdir() if d.is_dir())

        for name in unit_names:
            profile_unit = profile_base / name
            sync_unit = sync_base / name
            profile_mtime = self._dir_mtime(profile_unit) if profile_unit.exists() else 0.0
            sync_mtime = self._dir_mtime(sync_unit) if sync_unit.exists() else 0.0

            if profile_mtime == sync_mtime:
                continue

            if direction == "push":
                if profile_mtime > sync_mtime:
                    self.logger.info(
                        "LevelDB %s/%s: profile newer — copying to sync", subpath, name
                    )
                    sync_base.mkdir(parents=True, exist_ok=True)
                    self._copy_leveldb_atomic(profile_unit, sync_unit)
            elif direction == "pull":
                if sync_mtime > profile_mtime:
                    self.logger.info(
                        "LevelDB %s/%s: sync newer — copying to profile", subpath, name
                    )
                    profile_base.mkdir(parents=True, exist_ok=True)
                    self._copy_leveldb_atomic(sync_unit, profile_unit)
            else:  # both
                if profile_mtime > sync_mtime:
                    self.logger.info(
                        "LevelDB %s/%s: profile newer — copying to sync", subpath, name
                    )
                    sync_base.mkdir(parents=True, exist_ok=True)
                    self._copy_leveldb_atomic(profile_unit, sync_unit)
                else:
                    self.logger.info(
                        "LevelDB %s/%s: sync newer — copying to profile", subpath, name
                    )
                    profile_base.mkdir(parents=True, exist_ok=True)
                    self._copy_leveldb_atomic(sync_unit, profile_unit)

    # ------------------------------------------------------------------
    # Backup rotation
    # ------------------------------------------------------------------

    def _rotate_backups(self) -> None:
        """Rotate backup-1 → backup-2 (evicting backup-2) and current → backup-1."""
        backup2 = self.sync_folder / "backup-2"
        backup1 = self.sync_folder / "backup-1"
        current = self.sync_folder / "current"

        if backup2.exists():
            shutil.rmtree(backup2)
            self.logger.debug("Evicted backup-2")

        if backup1.exists():
            shutil.move(str(backup1), backup2)
            self.logger.debug("Renamed backup-1 → backup-2")

        if current.exists():
            shutil.copytree(current, backup1)
            self.logger.debug("Copied current → backup-1")

    # ------------------------------------------------------------------
    # External Extensions registration
    # ------------------------------------------------------------------

    def _install_external_extensions(self, sync_profile_path: Path, ext_dir: Path) -> None:
        """Write External Extensions JSON stubs for extensions present in the sync folder.

        Each stub tells the browser to install the extension from the Chrome Web Store
        on next launch.  Files are only written when they do not already exist, so
        repeated syncs are idempotent.
        """
        sync_ext_dir = sync_profile_path / "Extensions"
        if not sync_ext_dir.exists():
            return

        ext_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"external_update_url": "https://clients2.google.com/service/update2/crx"}
        )

        for entry in sync_ext_dir.iterdir():
            if not entry.is_dir():
                continue
            stub = ext_dir / f"{entry.name}.json"
            if not stub.exists():
                stub.write_text(payload, encoding="utf-8")
                self.logger.info("Registered external extension stub: %s", entry.name)

    # ------------------------------------------------------------------
    # Per-profile orchestration
    # ------------------------------------------------------------------

    def sync_browser_profile(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        direction: str = "both",
    ) -> None:
        """Sync a single browser profile against its mirror in the sync folder.

        *data_types* controls which categories are included.  ``None`` means
        all categories enabled (same as passing all-True dict).
        *direction* is one of "both", "push", or "pull".
        """
        dt = data_types or {}
        sync_extensions = dt.get("extensions", True)
        sync_bookmarks = dt.get("bookmarks", True)
        sync_dictionary = dt.get("custom_dictionary", True)
        sync_local_storage = dt.get("local_storage", True)
        sync_indexeddb = dt.get("indexeddb", True)

        sync_profile_path.mkdir(parents=True, exist_ok=True)
        self.logger.info("Syncing profile: %s ↔ %s", profile_path, sync_profile_path)

        if sync_extensions:
            self._sync_extensions(profile_path, sync_profile_path, direction)
            for subpath in ("Local Extension Settings", "Sync Extension Settings"):
                self._sync_leveldb_dir(profile_path, sync_profile_path, subpath, direction)

        if sync_local_storage:
            self._sync_leveldb_dir(
                profile_path, sync_profile_path, "Local Storage/leveldb", direction
            )

        if sync_indexeddb:
            self._sync_leveldb_dir(profile_path, sync_profile_path, "IndexedDB", direction)

        # Preferences is always synced (browser settings, not sensitive)
        plain_files = ["Preferences"]
        if sync_bookmarks:
            plain_files.append("Bookmarks")
        if sync_dictionary:
            plain_files.append("Custom Dictionary.txt")

        for filename in plain_files:
            src = profile_path / filename
            dst = sync_profile_path / filename
            if src.exists() or dst.exists():
                self._sync_file(src, dst, direction)

        self.logger.info("Profile sync complete: %s", profile_path.name)

    # ------------------------------------------------------------------
    # Top-level orchestration
    # ------------------------------------------------------------------

    def sync_all(self) -> None:
        """Sync all installed, non-running browsers, respecting saved config."""
        from src import config as _config

        enabled_browsers = _config.get_enabled_browsers()
        enabled_profiles = _config.get_enabled_profiles()
        data_types = _config.get_enabled_data_types()
        directions = _config.get_profile_directions()

        self.logger.info("Starting sync_all")
        self._rotate_backups()

        for browser in self.browsers:
            if not enabled_browsers.get(browser.name, True):
                self.logger.debug("Browser %s disabled in settings — skipping", browser.name)
                continue
            if not browser.is_installed():
                self.logger.debug("Browser %s not installed — skipping", browser.name)
                continue
            if browser.is_running():
                self.logger.warning(
                    "Browser %s is running — skipping to avoid data corruption",
                    browser.name,
                )
                continue

            profiles = browser.discover_profiles()
            if not profiles:
                self.logger.info("Browser %s: no profiles found", browser.name)
                continue

            allowed = enabled_profiles.get(browser.name)
            if allowed is not None:
                profiles = [p for p in profiles if p.name in allowed]
            if not profiles:
                self.logger.info("Browser %s: no enabled profiles — skipping", browser.name)
                continue

            ext_dir = browser.external_extensions_dir()
            self.logger.info("Browser %s: syncing %d profile(s)", browser.name, len(profiles))
            for profile_path in profiles:
                sync_profile_path = (
                    self.sync_folder / "current" / browser.name / profile_path.name
                )
                direction = directions.get(browser.name, {}).get(profile_path.name, "both")
                try:
                    self.sync_browser_profile(
                        profile_path, sync_profile_path, data_types, direction
                    )
                    if (
                        ext_dir is not None
                        and data_types.get("extensions", True)
                        and direction in ("pull", "both")
                    ):
                        self._install_external_extensions(sync_profile_path, ext_dir)
                except OSError:
                    self.logger.exception(
                        "Failed to sync profile %s for browser %s",
                        profile_path.name,
                        browser.name,
                    )

        self.update_metadata()
        self.logger.info("sync_all complete")

    def update_metadata(self) -> None:
        """Write metadata.json to the sync folder."""
        metadata = {
            "last_sync": datetime.now(tz=timezone.utc).isoformat(),
            "version": 1,
        }
        meta_path = self.sync_folder / "metadata.json"
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        self.logger.info("Metadata updated: %s", meta_path)
