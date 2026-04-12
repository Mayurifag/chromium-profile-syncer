from __future__ import annotations

import json
import logging
import re
import shutil
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.browsers.base import BrowserBase


def _get_rclone_fallback_paths() -> list[Path]:
    """Return platform-specific fallback paths for rclone."""
    if sys.platform == "darwin":
        return [Path("/opt/homebrew/bin/rclone"), Path("/usr/local/bin/rclone")]
    elif sys.platform == "win32":
        # Windows: Check Program Files and common install locations
        program_files = Path("C:/Program Files/rclone/rclone.exe")
        program_files_x86 = Path("C:/Program Files (x86)/rclone/rclone.exe")
        localappdata = Path.home() / "AppData" / "Local" / "rclone" / "rclone.exe"
        return [program_files, program_files_x86, localappdata]
    else:
        # Linux: common binary locations
        return [Path("/usr/bin/rclone"), Path("/usr/local/bin/rclone")]


_FALLBACK_PATHS = _get_rclone_fallback_paths()


@lru_cache(maxsize=1)
def find_rclone() -> Path | None:
    which = shutil.which("rclone")
    if which:
        return Path(which)
    for p in _FALLBACK_PATHS:
        if p.exists():
            return p
    return None


NEVER_SYNC: frozenset[str] = frozenset(
    ["Login Data", "Cookies", "Web Data", "History", "Secure Preferences"]
)

DEFAULT_DATA_TYPES: dict[str, bool] = {
    "extensions": True,
    "bookmarks": True,
    "custom_dictionary": True,
    "local_storage": True,
    "search_shortcuts": True,
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
        # Sync statistics
        self._synced_count: int = 0
        self._skipped_count: int = 0

    def _report(self, description: str) -> None:
        if self._progress_cb:
            self._progress_cb(description)

    def _rclone_sync(self, src: Path, dst: Path, description: str = "") -> None:
        """Sync src → dst using rclone with progress reporting.

        Uses rclone for fast parallel transfers with real-time progress.
        """
        self._report(f"{description} (starting...)" if description else "Starting sync...")

        cmd = [
            str(find_rclone() or "rclone"), "sync",
            str(src), str(dst),
            "--stats", "1s",           # Update stats every 1 second
            "--stats-one-line",        # Single line output for parsing
            "--transfers", "8",        # 8 parallel transfers
            "--checkers", "16",        # 16 parallel checksum threads
            "--exclude", "._*",        # Exclude macOS metadata files
            "--checksum",              # Use checksums instead of mod-time (skip unchanged files)
            "--fast-list",             # Use fewer transactions for faster operation
        ]

        # Log the full command being executed
        self.logger.debug("Executing: %s", " ".join(cmd))

        output_lines: list[str] = []

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

                output_lines.append(line)

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
                error_output = "\n".join(output_lines[-10:]) if output_lines else "No output"
                self.logger.error("rclone failed with output:\n%s", error_output)
                raise subprocess.CalledProcessError(process.returncode, cmd, output=error_output)

            self.logger.debug("rclone sync complete: %s → %s", src, dst)

        except subprocess.CalledProcessError as exc:
            self.logger.exception("rclone sync failed: %s → %s", src, dst)
            error_msg = (
                f"rclone sync failed: {exc.output}"
                if exc.output
                else f"rclone sync failed: {exc}"
            )
            raise OSError(error_msg) from exc
        except FileNotFoundError as exc:
            self.logger.exception("rclone not found")
            raise OSError(f"rclone not found: {exc}") from exc

    # ------------------------------------------------------------------
    # Low-level primitives
    # ------------------------------------------------------------------

    def _copy_leveldb_atomic(
        self, src: Path, dst: Path, *, display_name: str | None = None
    ) -> None:
        """Copy LevelDB directory src → dst atomically via a .tmp staging area.

        If the copy fails, dst is left untouched.  The .tmp dir may be left
        behind for manual cleanup but will never clobber dst.
        """
        tmp = dst.with_suffix(".tmp")
        try:
            if tmp.exists():
                shutil.rmtree(tmp)
            self._report(display_name or src.name)
            # Use ignore function to skip macOS metadata files
            shutil.copytree(src, tmp, ignore=shutil.ignore_patterns("._*"))
            if dst.exists():
                shutil.rmtree(dst)
            shutil.move(str(tmp), dst)
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
            self._skipped_count += 1
            return
        if direction == "push":
            if src_mtime > dst_mtime and src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                self._report(src.name)
                self.logger.info("Copying file: %s → %s", src.name, dst)
                shutil.copy2(src, dst)
                self._synced_count += 1
            else:
                self._skipped_count += 1
        elif direction == "pull":
            if dst_mtime > src_mtime and dst.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                self._report(dst.name)
                self.logger.info("Copying file: %s → %s", dst.name, src)
                shutil.copy2(dst, src)
                self._synced_count += 1
            else:
                self._skipped_count += 1
        else:  # both
            if src_mtime > dst_mtime:
                dst.parent.mkdir(parents=True, exist_ok=True)
                self._report(src.name)
                self.logger.info("Copying file: %s → %s", src.name, dst)
                shutil.copy2(src, dst)
                self._synced_count += 1
            else:
                src.parent.mkdir(parents=True, exist_ok=True)
                self._report(dst.name)
                self.logger.info("Copying file: %s → %s", dst.name, src)
                shutil.copy2(dst, src)
                self._synced_count += 1

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
            self.logger.info("Detected %d Web Store extensions (tracking by ID)", len(webstore_ids))

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
            self._skipped_count += 1
            return

        profile_ver = self._extension_dir_version(profile_best) if profile_best else (0,)
        sync_ver = self._extension_dir_version(sync_best) if sync_best else (0,)

        if profile_ver == sync_ver:
            self._skipped_count += 1
            return  # already in sync

        if profile_ver > sync_ver:
            if direction in ("push", "both") and profile_best is not None:
                dest = sync_id_dir / profile_best.name
                ext_name = self._get_extension_name(profile_best)
                self.logger.info(
                    "Extension %s (%s): unpacked extension, profile version %s > sync %s — syncing",
                    ext_name,
                    ext_id,
                    profile_ver,
                    sync_ver,
                )
                sync_id_dir.mkdir(parents=True, exist_ok=True)
                self._copy_leveldb_atomic(profile_best, dest, display_name=ext_name)
                self._synced_count += 1
            else:
                self._skipped_count += 1
        else:
            if direction in ("pull", "both") and sync_best is not None:
                dest = profile_id_dir / sync_best.name
                ext_name = self._get_extension_name(sync_best)
                self.logger.info(
                    "Extension %s (%s): unpacked extension, sync version %s > profile %s — syncing",
                    ext_name,
                    ext_id,
                    sync_ver,
                    profile_ver,
                )
                profile_id_dir.mkdir(parents=True, exist_ok=True)
                self._copy_leveldb_atomic(sync_best, dest, display_name=ext_name)
                self._synced_count += 1
            else:
                self._skipped_count += 1

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

    def _get_extension_name(self, version_dir: Path) -> str:
        """Get human-readable extension name from manifest.json.

        Falls back to extension ID if name not found.
        """
        manifest = version_dir / "manifest.json"
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                name = data.get("name", "")
                if name and not name.startswith("__MSG_"):
                    return name
            except (OSError, json.JSONDecodeError):
                pass
        return version_dir.parent.name

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
                self._skipped_count += 1
                return

            if direction == "push":
                if profile_mtime > sync_mtime:
                    sync_base.mkdir(parents=True, exist_ok=True)
                    self._copy_leveldb_atomic(profile_unit, sync_unit)
                    self._synced_count += 1
                else:
                    self._skipped_count += 1
            elif direction == "pull":
                if sync_mtime > profile_mtime:
                    profile_base.mkdir(parents=True, exist_ok=True)
                    self._copy_leveldb_atomic(sync_unit, profile_unit)
                    self._synced_count += 1
                else:
                    self._skipped_count += 1
            else:  # both
                if profile_mtime > sync_mtime:
                    sync_base.mkdir(parents=True, exist_ok=True)
                    self._copy_leveldb_atomic(profile_unit, sync_unit)
                    self._synced_count += 1
                else:
                    profile_base.mkdir(parents=True, exist_ok=True)
                    self._copy_leveldb_atomic(sync_unit, profile_unit)
                    self._synced_count += 1

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

    def restore_profile_from_backup(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        *,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        """Complete wipe and restore: delete local profile data, copy everything from backup.

        This is used for first-time profile management to ensure a clean slate.
        """
        self._progress_cb = on_progress
        dt = data_types or {}

        if not sync_profile_path.exists():
            self.logger.warning("Backup does not exist at %s — cannot restore", sync_profile_path)
            return

        self.logger.info("Restoring profile from backup: %s → %s", sync_profile_path, profile_path)

        # Delete local profile data (extensions, settings, bookmarks, etc.)
        items_to_delete = []

        if dt.get("extensions", True):
            items_to_delete.append(profile_path / "Extensions")
            items_to_delete.append(profile_path / "Local Extension Settings")
            items_to_delete.append(profile_path / "Sync Extension Settings")

        if dt.get("local_storage", True):
            items_to_delete.append(profile_path / "Local Storage")

        items_to_delete.append(profile_path / "Preferences")
        if dt.get("bookmarks", True):
            items_to_delete.append(profile_path / "Bookmarks")
        if dt.get("custom_dictionary", True):
            items_to_delete.append(profile_path / "Custom Dictionary.txt")

        # Delete existing items
        for item in items_to_delete:
            if item.exists():
                if item.is_dir():
                    self.logger.debug("Deleting directory: %s", item)
                    shutil.rmtree(item)
                else:
                    self.logger.debug("Deleting file: %s", item)
                    item.unlink()
                self._synced_count += 1

        # Copy everything from backup using rclone for efficiency
        self._report("Restoring from backup...")
        try:
            cmd = [
                str(find_rclone() or "rclone"), "copy",
                str(sync_profile_path), str(profile_path),
                "--stats", "1s",
                "--stats-one-line",
                "--transfers", "8",
                "--checkers", "16",
                "--exclude", "._*",
            ]

            self.logger.debug("Executing restore: %s", " ".join(cmd))

            process = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )

            for line in iter(process.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                match = re.search(r"Transferred:\s+[\d.]+\s*\w+\s*/\s*[\d.]+\s*\w+,\s*(\d+)%", line)
                if match:
                    pct = match.group(1)
                    self._report(f"Restoring ({pct}%)")

            process.wait()
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, cmd)

        except (subprocess.CalledProcessError, FileNotFoundError) as exc:
            self.logger.exception("Restore failed: %s", exc)
            raise OSError(f"Restore failed: {exc}") from exc

        if dt.get("search_shortcuts", True):
            self._restore_search_shortcuts(profile_path, self.sync_folder)

        self._progress_cb = None
        self.logger.info("Profile restore complete: %s", profile_path.name)

    # ------------------------------------------------------------------
    # Search shortcuts backup/restore
    # ------------------------------------------------------------------

    def _extract_search_shortcuts(self, profile_path: Path, sync_folder_root: Path) -> None:
        """Extract ACTIVE search shortcuts from Web Data database to global JSON file.

        Reads only active (is_active = 1) keywords from Web Data and exports to
        search_shortcuts.json at the root of sync folder (shared across all browsers).
        Uses read-only connection to avoid lock issues.
        Always creates the file even if extraction fails (empty array).
        """
        web_data_src = profile_path / "Web Data"
        shortcuts_json = sync_folder_root / "search_shortcuts.json"

        if not web_data_src.exists():
            self.logger.debug("No Web Data database found at %s", web_data_src)
            shortcuts_json.write_text("[]", encoding="utf-8")
            return

        try:
            conn = sqlite3.connect(f"file:{web_data_src}?mode=ro&immutable=1", uri=True)
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT keyword, short_name, url, favicon_url, suggest_url,
                       prepopulate_id, is_active, date_created, last_modified
                FROM keywords
                WHERE is_active = 1
                ORDER BY keyword
                """
            )
            rows = cursor.fetchall()
            conn.close()

            shortcuts = [
                {
                    "keyword": row[0],
                    "short_name": row[1],
                    "url": row[2],
                    "favicon_url": row[3],
                    "suggest_url": row[4],
                    "prepopulate_id": row[5],
                    "is_active": row[6],
                    "date_created": row[7],
                    "last_modified": row[8],
                }
                for row in rows
            ]

            shortcuts_json.write_text(json.dumps(shortcuts, indent=2), encoding="utf-8")
            self._report("search_shortcuts.json")
            self.logger.info(
                "Extracted %d active search shortcuts to %s", len(shortcuts), shortcuts_json
            )

        except sqlite3.Error as exc:
            self.logger.warning(
                "Failed to extract search shortcuts from %s: %s (creating empty file)",
                web_data_src,
                exc,
            )
            shortcuts_json.write_text("[]", encoding="utf-8")

    def _restore_search_shortcuts(self, profile_path: Path, sync_folder_root: Path) -> None:
        """Restore search shortcuts from global JSON file to Web Data database (overwrite mode).

        Reads search_shortcuts.json from sync folder root and overwrites all keywords
        in the Web Data database. This removes non-active shortcuts.
        """
        web_data_dst = profile_path / "Web Data"
        shortcuts_json = sync_folder_root / "search_shortcuts.json"

        if not shortcuts_json.exists():
            self.logger.debug("No search_shortcuts.json found at %s", shortcuts_json)
            return

        if not web_data_dst.exists():
            self.logger.warning("Web Data database not found at %s — cannot restore", web_data_dst)
            return

        try:
            shortcuts = json.loads(shortcuts_json.read_text(encoding="utf-8"))

            conn = sqlite3.connect(str(web_data_dst))
            cursor = conn.cursor()

            cursor.execute("DELETE FROM keywords")

            for shortcut in shortcuts:
                cursor.execute(
                    """
                    INSERT INTO keywords (
                        keyword, short_name, url, favicon_url, suggest_url,
                        prepopulate_id, is_active, date_created, last_modified
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        shortcut["keyword"],
                        shortcut["short_name"],
                        shortcut["url"],
                        shortcut.get("favicon_url", ""),
                        shortcut.get("suggest_url", ""),
                        shortcut.get("prepopulate_id", 0),
                        shortcut.get("is_active", 1),
                        shortcut.get("date_created", 0),
                        shortcut.get("last_modified", 0),
                    ),
                )

            conn.commit()
            conn.close()

            self._report("search shortcuts restored")
            self.logger.info("Restored %d search shortcuts from %s", len(shortcuts), shortcuts_json)

        except (sqlite3.Error, json.JSONDecodeError, KeyError) as exc:
            self.logger.warning("Failed to restore search shortcuts: %s", exc)

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

        if dt.get("search_shortcuts", True):
            if direction in ("push", "both"):
                self._extract_search_shortcuts(profile_path, self.sync_folder)
            if direction in ("pull", "both"):
                self._restore_search_shortcuts(profile_path, self.sync_folder)

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
        profiles_needing_restore = _config.get_profiles_needing_restore()
        data_types = DEFAULT_DATA_TYPES

        # Detect first-time sync (no metadata.json exists yet)
        meta_path = self.sync_folder / "metadata.json"
        is_first_sync = not meta_path.exists()

        # Reset sync statistics
        self._synced_count = 0
        self._skipped_count = 0

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

                # Check if this profile needs initial restore from backup
                needs_restore = (
                    browser.name in profiles_needing_restore
                    and profile_path.name in profiles_needing_restore[browser.name]
                )

                try:
                    if needs_restore:
                        # Complete wipe and restore from backup
                        self.logger.info(
                            "Profile %s/%s: performing complete restore from backup",
                            browser.name,
                            profile_path.name,
                        )
                        self.restore_profile_from_backup(
                            profile_path, sync_profile_path, data_types
                        )
                        if ext_dir is not None:
                            self._install_external_extensions(sync_profile_path, ext_dir)
                        _config.clear_restore_flag(browser.name, profile_path.name)
                    else:
                        # Normal bidirectional sync
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

        # Log summary
        summary = (
            f"Synced: {self._synced_count} items, "
            f"Skipped: {self._skipped_count} items (unchanged)"
        )
        if is_first_sync:
            self.logger.info("Initial sync complete — %s", summary)
        else:
            self.logger.info("Sync complete — %s", summary)

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
