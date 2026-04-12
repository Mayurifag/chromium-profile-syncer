from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

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
    "indexeddb": False,  # Website caches - typically not needed
}

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
        self._progress_cb: Callable[[str], None] | None = None

    def _report(self, description: str) -> None:
        if self._progress_cb:
            self._progress_cb(description)

    def _rclone_sync(self, src: Path, dst: Path, description: str = "") -> None:
        """Sync src → dst using rclone with progress reporting.

        Uses rclone for fast parallel transfers with real-time progress.
        """
        self._report(f"{description} (starting...)" if description else "Starting sync...")

        cmd = [
            "rclone", "sync",
            str(src), str(dst),
            "--stats", "1s",           # Update stats every 1 second
            "--stats-one-line",        # Single line output for parsing
            "--transfers", "8",        # 8 parallel transfers
            "--checkers", "16",        # 16 parallel checksum threads
            "--exclude", "._*",        # Exclude macOS metadata files
        ]

        # Log the full command being executed
        self.logger.info("Executing: %s", " ".join(cmd))

        try:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            # Parse progress output
            for line in iter(process.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue

                # Parse rclone stats line
                # Example: "Transferred: 123.456 MiB / 500 MiB, 25%, 10 MiB/s, ETA 30s"
                match = re.search(r"Transferred:\s+[\d.]+\s*\w+\s*/\s*[\d.]+\s*\w+,\s*(\d+)%", line)
                if match:
                    pct = match.group(1)
                    status = f"{description} ({pct}%)" if description else f"Syncing ({pct}%)"
                    self._report(status)
                elif "Transferred:" in line:
                    # Show abbreviated status if we can't parse percentage
                    self._report(f"{description}..." if description else "Syncing...")

            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)

            self.logger.debug("rclone sync complete: %s → %s", src, dst)

        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            self.logger.exception("rclone sync failed: %s → %s", src, dst)
            raise OSError(f"rclone sync failed: {exc}") from exc

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
            self._report(src.name)
            self.logger.info("Copying LevelDB: %s → %s", src.name, dst)
            # Use ignore function to skip macOS metadata files
            shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("._*"))
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(tmp), dst)
            self.logger.debug("Atomic copy complete: %s → %s", src, dst)
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
                self._report(src.name)
                self.logger.info("Copying file: %s → %s", src.name, dst)
                shutil.copy2(src, dst)
        elif direction == "pull":
            if dst_mtime > src_mtime and dst.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                self._report(dst.name)
                self.logger.info("Copying file: %s → %s", dst.name, src)
                shutil.copy2(dst, src)
        else:  # both
            if src_mtime > dst_mtime:
                dst.parent.mkdir(parents=True, exist_ok=True)
                self._report(src.name)
                self.logger.info("Copying file: %s → %s", src.name, dst)
                shutil.copy2(src, dst)
            else:
                src.parent.mkdir(parents=True, exist_ok=True)
                self._report(dst.name)
                self.logger.info("Copying file: %s → %s", dst.name, src)
                shutil.copy2(dst, src)

    # ------------------------------------------------------------------
    # Extension sync (version-based)
    # ------------------------------------------------------------------

    def _is_webstore_extension(self, version_dir: Path) -> bool:
        """Check if an extension version is from Chrome Web Store.

        Web Store extensions have _metadata/verified_contents.json with signatures.
        Unpacked/developer extensions don't have this file.
        """
        return (version_dir / "_metadata" / "verified_contents.json").exists()

    def _sync_extensions(self, profile_dir: Path, sync_dir: Path, direction: str = "both") -> None:
        """Sync Extensions/ — only sync unpacked extensions, track Web Store ones in manifest."""
        profile_ext_dir = profile_dir / "Extensions"
        sync_ext_dir = sync_dir / "Extensions"

        # Collect all extension IDs from both sides
        ext_ids: set[str] = set()
        if profile_ext_dir.exists():
            ext_ids.update(d.name for d in profile_ext_dir.iterdir() if d.is_dir())
        if sync_ext_dir.exists():
            ext_ids.update(d.name for d in sync_ext_dir.iterdir() if d.is_dir())

        # Track Web Store extension IDs in a manifest file
        webstore_ids: set[str] = set()
        manifest_path = sync_dir / "webstore_extensions.json"

        # Load existing manifest
        if manifest_path.exists():
            try:
                webstore_ids = set(json.loads(manifest_path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(
                    self._sync_extension_id,
                    profile_ext_dir / ext_id,
                    sync_ext_dir / ext_id,
                    ext_id,
                    direction,
                    webstore_ids,
                ): ext_id
                for ext_id in ext_ids
            }
            for fut in as_completed(futures):
                fut.result()

        # Save updated manifest
        if webstore_ids:
            sync_dir.mkdir(parents=True, exist_ok=True)
            manifest_path.write_text(json.dumps(sorted(webstore_ids)), encoding="utf-8")
            self.logger.debug("Saved Web Store extensions manifest: %d IDs", len(webstore_ids))

    def _sync_extension_id(
        self,
        profile_id_dir: Path,
        sync_id_dir: Path,
        ext_id: str,
        direction: str,
        webstore_ids: set[str],
    ) -> None:
        """Sync one extension ID directory by picking the higher version.

        Only syncs unpacked/developer extensions (no Web Store signature).
        Web Store extensions are tracked in webstore_ids set for external registration.
        """
        # Find best version dir on each side
        profile_best = self._best_extension_version_dir(profile_id_dir)
        sync_best = self._best_extension_version_dir(sync_id_dir)

        if profile_best is None and sync_best is None:
            return

        # Check if this is a Web Store extension (skip syncing the code, just track ID)
        check_dir = profile_best if profile_best else sync_best
        if check_dir and self._is_webstore_extension(check_dir):
            webstore_ids.add(ext_id)
            self.logger.debug(
                "Extension %s: Web Store extension — tracking ID (will register by ID)",
                ext_id,
            )
            return

        profile_ver = self._extension_dir_version(profile_best) if profile_best else (0,)
        sync_ver = self._extension_dir_version(sync_best) if sync_best else (0,)

        if profile_ver == sync_ver:
            return  # already in sync

        if profile_ver > sync_ver:
            if direction in ("push", "both") and profile_best is not None:
                dest = sync_id_dir / profile_best.name
                self.logger.info(
                    "Extension %s: unpacked extension, profile version %s > sync %s — syncing",
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
                    "Extension %s: unpacked extension, sync version %s > profile %s — syncing",
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

        def _sync_unit(name: str) -> None:
            profile_unit = profile_base / name
            sync_unit = sync_base / name
            profile_mtime = self._dir_mtime(profile_unit) if profile_unit.exists() else 0.0
            sync_mtime = self._dir_mtime(sync_unit) if sync_unit.exists() else 0.0

            if profile_mtime == sync_mtime:
                return

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

        with ThreadPoolExecutor(max_workers=8) as pool:
            for fut in as_completed({pool.submit(_sync_unit, n): n for n in unit_names}):
                fut.result()

    # ------------------------------------------------------------------
    # Backup rotation
    # ------------------------------------------------------------------

    def _rotate_backups(self) -> None:
        """Rotate backup-1 → backup-2 (evicting backup-2) and current → backup-1."""
        backup2 = self.sync_folder / "backup-2"
        backup1 = self.sync_folder / "backup-1"
        current = self.sync_folder / "current"

        if backup2.exists():
            self._report("Removing old backup-2...")
            shutil.rmtree(backup2)
            self.logger.debug("Evicted backup-2")

        if backup1.exists():
            self._report("Rotating backup-1 → backup-2...")
            shutil.move(str(backup1), backup2)
            self.logger.debug("Renamed backup-1 → backup-2")

        if current.exists():
            # Use rclone for fast parallel copy with progress
            self._rclone_sync(current, backup1, "Creating backup")
            self.logger.debug("Copied current → backup-1")

    # ------------------------------------------------------------------
    # External Extensions registration
    # ------------------------------------------------------------------

    def _install_external_extensions(self, sync_profile_path: Path, ext_dir: Path) -> None:
        """Write External Extensions JSON stubs for Web Store extensions from manifest.

        Reads webstore_extensions.json manifest to find which extensions to register.
        Each stub tells the browser to install the extension from the Chrome Web Store
        on next launch. Unpacked extensions are synced directly via file copy.
        """
        manifest_path = sync_profile_path / "webstore_extensions.json"
        if not manifest_path.exists():
            return

        try:
            ext_ids = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self.logger.warning("Failed to read webstore_extensions.json")
            return

        if not ext_ids:
            return

        ext_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"external_update_url": "https://clients2.google.com/service/update2/crx"}
        )

        for ext_id in ext_ids:
            stub = ext_dir / f"{ext_id}.json"
            if not stub.exists():
                stub.write_text(payload, encoding="utf-8")
                self.logger.info("Registered Web Store extension: %s", ext_id)

    # ------------------------------------------------------------------
    # Per-profile orchestration
    # ------------------------------------------------------------------

    def sync_browser_profile(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        *,
        direction: str = "both",
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        """Sync a single browser profile against its mirror in the sync folder.

        *data_types* controls which file categories are synced (all enabled by default).
        *direction* is one of "both", "push", or "pull".
        *on_progress* is called with a short description each time a file/dir is copied.
        """
        self._progress_cb = on_progress
        dt = data_types or {}
        sync_profile_path.mkdir(parents=True, exist_ok=True)
        self.logger.info("Syncing profile: %s ↔ %s", profile_path, sync_profile_path)

        if dt.get("extensions", True):
            self._sync_extensions(profile_path, sync_profile_path, direction)
            for subpath in ("Local Extension Settings", "Sync Extension Settings"):
                self._sync_leveldb_dir(profile_path, sync_profile_path, subpath, direction)

        if dt.get("local_storage", True):
            self._sync_leveldb_dir(
                profile_path, sync_profile_path, "Local Storage/leveldb", direction
            )

        if dt.get("indexeddb", False):
            self._sync_leveldb_dir(profile_path, sync_profile_path, "IndexedDB", direction)

        plain_files: list[tuple[str, str | None]] = [
            ("Preferences", None),
            ("Bookmarks", "bookmarks"),
            ("Custom Dictionary.txt", "custom_dictionary"),
        ]
        for filename, key in plain_files:
            if key is not None and not dt.get(key, True):
                continue
            src = profile_path / filename
            dst = sync_profile_path / filename
            if src.exists() or dst.exists():
                self._sync_file(src, dst, direction)

        self._progress_cb = None
        self.logger.info("Profile sync complete: %s", profile_path.name)

    # ------------------------------------------------------------------
    # Top-level orchestration
    # ------------------------------------------------------------------

    def sync_all(self) -> dict[str, bool]:
        """Sync all installed, non-running browsers, respecting saved config.

        Returns a dict with 'is_first_sync' to indicate if this was initial setup.
        """
        from src import config as _config

        enabled_browsers = _config.get_enabled_browsers()
        enabled_profiles = _config.get_enabled_profiles()
        directions = _config.get_profile_directions()
        data_types = DEFAULT_DATA_TYPES

        # Detect first-time sync (no metadata.json exists yet)
        meta_path = self.sync_folder / "metadata.json"
        is_first_sync = not meta_path.exists()

        if is_first_sync:
            self.logger.info("Starting initial sync (first-time setup)")
        else:
            self.logger.info("Starting sync_all")

        self._rotate_backups()

        for browser in self.browsers:
            if enabled_browsers.get(browser.name) is False:
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
            if allowed is None or not allowed:
                self.logger.info(
                    "Browser %s: no enabled profiles in config — skipping", browser.name
                )
                continue

            profiles = [p for p in profiles if p.name in allowed]
            if not profiles:
                self.logger.info("Browser %s: no enabled profiles — skipping", browser.name)
                continue

            ext_dir = browser.external_extensions_dir()
            self.logger.info("Browser %s: syncing %d profile(s)", browser.name, len(profiles))
            for profile_path in profiles:
                self._report(f"{browser.name}/{profile_path.name}")
                sync_profile_path = (
                    self.sync_folder / "current" / browser.name / profile_path.name
                )
                direction = directions.get(browser.name, {}).get(profile_path.name, "both")
                try:
                    self.sync_browser_profile(
                        profile_path, sync_profile_path, data_types, direction=direction
                    )
                    if ext_dir is not None and direction in ("pull", "both"):
                        self._install_external_extensions(sync_profile_path, ext_dir)
                except OSError:
                    self.logger.exception(
                        "Failed to sync profile %s for browser %s",
                        profile_path.name,
                        browser.name,
                    )

        self.update_metadata()
        if is_first_sync:
            self.logger.info("Initial sync complete")
        else:
            self.logger.info("sync_all complete")

        return {"is_first_sync": is_first_sync}

    def update_metadata(self) -> None:
        """Write metadata.json to the sync folder."""
        metadata = {
            "last_sync": datetime.now(tz=UTC).isoformat(),
            "version": 1,
        }
        meta_path = self.sync_folder / "metadata.json"
        meta_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")
        self.logger.info("Metadata updated: %s", meta_path)
