from __future__ import annotations

import hashlib
import json
import logging
import os
import platform
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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

# Dotted key paths extracted from Preferences and stored as preferences.json in the archive.
# Only keys with actual data in the source profile are written; on restore, only keys that
# already exist in the target Preferences are updated (never injected from scratch).
PREFERENCES_KEYS: tuple[str, ...] = (
    # Privacy & security
    "enable_do_not_track",
    "https_only_mode_enabled",
    "safe_browsing",
    "safebrowsing",
    "net.network_prediction_options",
    # Password manager
    "credentials_enable_service",
    "credentials_enable_autosignin",
    # Language & input
    "intl",
    "spellcheck",
    "translate_blocked_languages",
    "translate_allowlists",
    "translate_recent_target",
    # Search & omnibox
    "search",
    "omnibox",
    # UI chrome
    "toolbar",
    "bookmark_bar",
    "browser.enable_spellchecking",
    "browser.theme",
    # File dialogs
    "savefile",
    "selectfile",
    # DevTools layout
    "devtools.preferences",
    # Per-site zoom
    "partition.per_host_zoom_levels",
    # Protocol handlers
    "custom_handlers",
    # Per-site permission grants (exclude engagement/metadata noise)
    "profile.content_settings.exceptions.geolocation",
    "profile.content_settings.exceptions.notifications",
    "profile.content_settings.exceptions.media_stream_mic",
    "profile.content_settings.exceptions.media_stream_camera",
    "profile.content_settings.exceptions.popups",
    "profile.content_settings.exceptions.http_allowed",
    "profile.content_settings.exceptions.javascript",
    "profile.content_settings.exceptions.cookies",
    "profile.content_settings.exceptions.sound",
    "profile.content_settings.exceptions.autoplay",
    "profile.content_settings.exceptions.automatic_downloads",
    "profile.content_settings.exceptions.window_placement",
    "profile.content_settings.exceptions.hid_chooser_data",
    "profile.content_settings.exceptions.ssl_cert_decisions",
    "profile.content_settings.exceptions.fedcm_idp_signin",
)


def _get_nested(d: dict, keys: list[str]) -> tuple[bool, object]:
    cur: object = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return False, None
        cur = cur[k]
    return True, cur


def _set_nested(d: dict, keys: list[str], value: object) -> None:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def _merge_prefs(target: dict, source: dict) -> None:
    """Deep-merge *source* into *target*, updating only keys that already exist in *target*."""
    for key, value in source.items():
        if key not in target:
            continue
        if isinstance(value, dict) and isinstance(target[key], dict):
            _merge_prefs(target[key], value)
        else:
            target[key] = value


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
        self,
        profile_dir: Path,
        sync_dir: Path,
        subpath: str,
        direction: str = "both",
        name_filter: Callable[[str], bool] | None = None,
    ) -> None:
        """For each LevelDB unit under *subpath*, copy according to direction atomically."""
        profile_base = profile_dir / subpath
        sync_base = sync_dir / subpath

        unit_names: set[str] = set()
        if profile_base.exists():
            unit_names.update(d.name for d in profile_base.iterdir() if d.is_dir())
        if sync_base.exists():
            unit_names.update(d.name for d in sync_base.iterdir() if d.is_dir())
        if name_filter is not None:
            unit_names = {n for n in unit_names if name_filter(n)}

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
    # Archive helpers
    # ------------------------------------------------------------------

    def _validate_archive_content(self, work_dir: Path) -> bool:
        """Check each expected data type independently and log per-item status.

        Returns False only when the staging directory contains no meaningful
        content at all (i.e. profile discovery failed entirely), to prevent
        overwriting the cloud archive with empty data.  Missing individual items
        are logged as warnings so silent partial-backup failures are visible.
        """
        def _dir_nonempty(p: Path) -> bool:
            return p.is_dir() and any(p.iterdir())

        checks: dict[str, bool] = {
            "extensions (unpacked)": _dir_nonempty(work_dir / "Extensions"),
            "extensions (webstore manifest)": (work_dir / "webstore_extensions.json").is_file(),
            "local_extension_settings": _dir_nonempty(work_dir / "Local Extension Settings"),
            "indexed_db (extensions)": (work_dir / "IndexedDB").is_dir() and any(
                d for d in (work_dir / "IndexedDB").iterdir()
                if d.is_dir() and d.name.startswith("chrome-extension_")
            ),
            "local_storage": _dir_nonempty(work_dir / "Local Storage" / "leveldb"),
            "bookmarks": (work_dir / "Bookmarks").is_file(),
            "custom_dictionary": (work_dir / "Custom Dictionary.txt").is_file(),
            "preferences": (work_dir / "preferences.json").is_file(),
            "search_shortcuts": (work_dir / "search_shortcuts.json").is_file(),
        }

        for item, present in checks.items():
            if present:
                self.logger.debug("Archive check OK: %s", item)
            else:
                self.logger.warning("Archive check MISSING: %s", item)

        present_items = [k for k, v in checks.items() if v]
        missing_items = [k for k, v in checks.items() if not v]

        if missing_items:
            self.logger.warning(
                "Archive integrity: %d/%d items present, missing: %s",
                len(present_items),
                len(checks),
                ", ".join(missing_items),
            )

        if not present_items:
            self.logger.error(
                "Archive integrity check failed: no expected items found in staging dir "
                "— skipping pack to avoid overwriting cloud archive with empty data"
            )
            return False

        return True

    def _pack_to_archive(self, src_dir: Path, dst_archive: Path) -> None:
        """Pack src_dir into an uncompressed tar archive at dst_archive.

        Writes to a system temp file first (outside the sync folder) to avoid
        triggering cloud-sync clients mid-write, then moves into place.
        Tarfile preserves file modification times exactly (float precision), which
        is essential for the mtime-based sync comparisons used elsewhere.
        """
        with tempfile.NamedTemporaryFile(suffix=".tar.tmp", delete=False) as ntf:
            tmp = Path(ntf.name)
        try:
            with tarfile.open(str(tmp), "w:") as tf:
                tf.add(str(src_dir), arcname=".")
            for attempt in range(20):
                try:
                    shutil.copy2(str(tmp), dst_archive)
                    break
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.5)
        finally:
            tmp.unlink(missing_ok=True)

    def _unpack_archive(self, src_archive: Path, dst_dir: Path) -> None:
        """Unpack a tar archive into dst_dir, restoring file modification times."""
        dst_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(str(src_archive)) as tf:
            tf.extractall(str(dst_dir), filter="data")

    # ------------------------------------------------------------------
    # External Extensions registration
    # ------------------------------------------------------------------

    def _install_external_extensions(self, sync_profile_path: Path, browser: object) -> None:
        """Register Web Store extensions from manifest for the given browser.

        On Windows, Chrome ignores the file-based External Extensions directory and
        only reads from the Registry. Browsers that provide windows_extensions_registry_key()
        use HKCU registry entries; others fall back to file-based JSON stubs (works on
        macOS, Linux, and non-Chrome browsers that honour the directory).

        Extensions in the ungoogled_only_extensions config list are skipped for browsers
        that are not marked as ungoogled (they have the feature built-in instead).
        """
        from src import config as _config

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

        is_ungoogled = getattr(browser, "ungoogled", True)
        if not is_ungoogled:
            ungoogled_only = set(_config.get_ungoogled_only_extensions())
            before = len(ext_ids)
            ext_ids = [e for e in ext_ids if e not in ungoogled_only]
            skipped = before - len(ext_ids)
            if skipped:
                self.logger.info(
                    "Skipping %d ungoogled-only extension(s) for non-ungoogled browser %s",
                    skipped,
                    getattr(browser, "name", "unknown"),
                )

        update_url = "https://clients2.google.com/service/update2/crx"

        on_windows = platform.system() == "Windows"
        reg_key = browser.windows_extensions_registry_key() if on_windows else None
        if reg_key:
            self._install_extensions_via_registry(ext_ids, reg_key, update_url)
            force_key = browser.windows_force_list_registry_key() if on_windows else None
            if force_key:
                self._install_extensions_via_force_list(ext_ids, force_key, update_url)
            # Clean up any file-based stubs that may have been written before switching
            # to the registry path, so the browser doesn't process them a second time.
            ext_dir = browser.external_extensions_dir()
            if ext_dir is not None and ext_dir.exists():
                for stub in ext_dir.glob("*.json"):
                    stub.unlink(missing_ok=True)
                    self.logger.info(
                        "Removed orphaned extension stub (now using registry): %s", stub.stem
                    )
        else:
            ext_dir = browser.external_extensions_dir()
            if ext_dir is not None:
                self._install_extensions_via_stubs(ext_ids, ext_dir, update_url)

    def _install_extensions_via_registry(
        self, ext_ids: list[str], reg_subkey: str, update_url: str
    ) -> None:
        """Write HKCU registry entries for Web Store extension auto-install on Windows.

        Also removes stale entries for IDs no longer in the manifest so a restore
        does not re-install extensions that were dropped from the backup.
        """
        import winreg  # Windows-only stdlib module

        ext_id_set = set(ext_ids)
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_subkey) as base:
                existing = []
                i = 0
                while True:
                    try:
                        existing.append(winreg.EnumKey(base, i))
                        i += 1
                    except OSError:
                        break
            for old_id in existing:
                if old_id not in ext_id_set:
                    try:
                        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{reg_subkey}\{old_id}")
                        self.logger.info("Removed stale registry extension: %s", old_id)
                    except OSError:
                        self.logger.warning("Failed to remove stale registry extension: %s", old_id)
        except FileNotFoundError:
            pass

        for ext_id in ext_ids:
            key_path = rf"{reg_subkey}\{ext_id}"
            try:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                    winreg.SetValueEx(key, "update_url", 0, winreg.REG_SZ, update_url)
                self.logger.info("Registered Web Store extension via registry: %s", ext_id)
            except OSError:
                self.logger.warning("Failed to register extension in registry: %s", ext_id)

    def _install_extensions_via_force_list(
        self, ext_ids: list[str], force_key: str, update_url: str
    ) -> None:
        """Write HKCU ExtensionInstallForcelist policy entries so extensions auto-enable.

        Clears existing entries before writing so stale IDs don't persist.
        """
        import winreg  # Windows-only stdlib module

        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, force_key) as key:
                value_names = []
                i = 0
                while True:
                    try:
                        value_names.append(winreg.EnumValue(key, i)[0])
                        i += 1
                    except OSError:
                        break
                for name in value_names:
                    winreg.DeleteValue(key, name)
                for i, ext_id in enumerate(ext_ids, start=1):
                    winreg.SetValueEx(key, str(i), 0, winreg.REG_SZ, f"{ext_id};{update_url}")
            self.logger.info("Wrote ExtensionInstallForcelist with %d entries", len(ext_ids))
        except OSError:
            self.logger.warning("Failed to write ExtensionInstallForcelist")

    def _install_extensions_via_stubs(
        self, ext_ids: list[str], ext_dir: Path, update_url: str
    ) -> None:
        """Write JSON stub files to the External Extensions directory.

        Also removes stale stubs for IDs no longer in the manifest.
        """
        ext_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"external_update_url": update_url})
        ext_id_set = set(ext_ids)

        for stub in ext_dir.glob("*.json"):
            if stub.stem not in ext_id_set:
                stub.unlink(missing_ok=True)
                self.logger.info("Removed stale extension stub: %s", stub.stem)

        for ext_id in ext_ids:
            stub = ext_dir / f"{ext_id}.json"
            if not stub.exists():
                stub.write_text(payload, encoding="utf-8")
                self.logger.info("Registered Web Store extension via stub: %s", ext_id)

    # ------------------------------------------------------------------
    # Per-profile orchestration
    # ------------------------------------------------------------------

    def restore_profile_from_backup(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        *,
        browser: object | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        """Complete wipe and restore: delete local profile data, copy everything from backup.

        This is used for first-time profile management to ensure a clean slate.
        Extensions and their settings/caches are fully wiped before restore so no
        stale data from a previous install persists.

        If *browser* is provided and not ungoogled, extensions listed in
        ungoogled_only_extensions config are excluded from the restore.
        """
        from src import config as _config

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
            items_to_delete.append(profile_path / "Extension State")
            items_to_delete.append(profile_path / "Extension Rules")
            indexed_db = profile_path / "IndexedDB"
            if indexed_db.exists():
                for entry in indexed_db.iterdir():
                    if entry.is_dir() and entry.name.startswith("chrome-extension_"):
                        items_to_delete.append(entry)

        if dt.get("local_storage", True):
            items_to_delete.append(profile_path / "Local Storage")

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

        # Determine which extension IDs to skip for non-ungoogled browsers
        is_ungoogled = getattr(browser, "ungoogled", True)
        excluded_ext_ids: list[str] = (
            [] if is_ungoogled else _config.get_ungoogled_only_extensions()
        )

        # Copy everything from backup using rclone (skips unchanged files)
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
                "--exclude", "preferences.json",
            ]
            for ext_id in excluded_ext_ids:
                cmd += ["--exclude", f"Extensions/{ext_id}/**"]
                cmd += ["--exclude", f"Local Extension Settings/{ext_id}/**"]
                cmd += ["--exclude", f"IndexedDB/chrome-extension_{ext_id}_*/**"]

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

        json_path = sync_profile_path / "preferences.json"
        prefs_path = profile_path / "Preferences"
        if json_path.exists() and prefs_path.exists():
            saved = json.loads(json_path.read_bytes())
            prefs = json.loads(prefs_path.read_bytes())
            if dt.get("extensions", True):
                # Clear the browser's pre-existing extension registry so that
                # browser-bundled extensions (e.g. Helium's built-in uBlock)
                # don't survive the restore.  Extensions in the backup manifest
                # will be re-registered via registry/stubs and Chromium will
                # re-populate extensions.settings on next launch.
                prefs.get("extensions", {}).pop("settings", None)
            _merge_prefs(prefs, saved)
            prefs_path.write_bytes(json.dumps(prefs, separators=(",", ":")).encode())
            self.logger.info("Merged preferences.json into %s", prefs_path)

        if dt.get("search_shortcuts", True):
            self._restore_search_shortcuts(profile_path, sync_profile_path)

        self.logger.info("Profile restore complete: %s", profile_path.name)

    # ------------------------------------------------------------------
    # Search shortcuts backup/restore
    # ------------------------------------------------------------------

    @staticmethod
    def _load_oscrypt_key(user_data_dir: Path) -> AESGCM | None:
        """Return an AESGCM cipher seeded from the browser's OSCrypt key (Windows only).

        On non-Windows platforms Chromium does not verify url_hash, so returns None.
        Returns None on any error so callers can skip url_hash computation gracefully.
        """
        if platform.system() != "Windows":
            return None
        try:
            import base64
            import ctypes
            import ctypes.wintypes

            local_state = json.loads(
                (user_data_dir / "Local State").read_text(encoding="utf-8")
            )
            enc_key = base64.b64decode(local_state["os_crypt"]["encrypted_key"])[5:]

            class _BLOB(ctypes.Structure):
                _fields_ = [
                    ("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char)),
                ]

            buf = ctypes.create_string_buffer(enc_key)
            blob_in = _BLOB(len(enc_key), buf)
            blob_out = _BLOB()
            ok = ctypes.windll.crypt32.CryptUnprotectData(
                ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
            )
            if not ok:
                return None
            return AESGCM(ctypes.string_at(blob_out.pbData, blob_out.cbData))
        except Exception:
            return None

    @staticmethod
    def _make_url_hash(row_id: int, url: str, aesgcm: AESGCM) -> bytes:
        """Compute Chromium's url_hash blob for a keyword row.

        Chromium stores SHA-256 of Pickle(WriteInt64(id), WriteString(url)),
        prefixed with a version byte, OSCrypt-encrypted as an AES-256-GCM blob.
        The DB row id is part of the hash so tampered rows cannot be reinserted
        with a different id.
        """
        url_b = url.encode("utf-8")
        pad = (4 - len(url_b) % 4) % 4
        payload = struct.pack("<q", row_id) + struct.pack("<I", len(url_b)) + url_b + bytes(pad)
        pickle_bytes = struct.pack("<I", len(payload)) + payload
        plaintext = b"\x01" + hashlib.sha256(pickle_bytes).digest()
        nonce = os.urandom(12)
        return b"v10" + nonce + aesgcm.encrypt(nonce, plaintext, None)

    def _extract_search_shortcuts(self, profile_path: Path, sync_folder_root: Path) -> None:
        """Extract user-created search shortcuts from Web Data database to global JSON file.

        Reads all user-created (prepopulate_id = 0) keywords from Web Data and writes them
        to search_shortcuts.json at the root of sync folder (shared across all browsers).
        This is only called for browsers in push direction — the JSON acts as the master
        and is consumed (but never overwritten) by browsers in both/pull direction.
        Uses read-only connection to avoid lock issues.

        When the default engine has an empty sync_guid in the DB but its URL matches
        default_search_provider_data in Preferences, the Preferences guid is adopted so
        it survives round-trip through JSON.
        """
        web_data_src = profile_path / "Web Data"
        shortcuts_json = sync_folder_root / "search_shortcuts.json"

        if not web_data_src.exists():
            self.logger.debug("No Web Data database found at %s — skipping extract", web_data_src)
            return

        try:
            prefs_path = profile_path / "Preferences"
            default_guid = ""
            default_engine_url = ""
            if prefs_path.exists():
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
                default_guid = prefs.get("default_search_provider", {}).get("guid", "")
                dspd = prefs.get("default_search_provider_data", {}).get(
                    "mirrored_template_url_data", {}
                )
                default_engine_url = dspd.get("url", "")

            conn = sqlite3.connect(f"file:{web_data_src}?mode=ro&immutable=1", uri=True)
            try:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    SELECT keyword, short_name, url, favicon_url, suggest_url,
                           prepopulate_id, is_active, date_created, last_modified,
                           sync_guid, safe_for_autoreplace, input_encodings, alternate_urls
                    FROM keywords
                    WHERE prepopulate_id = 0
                    ORDER BY keyword
                    """
                )
                rows = cursor.fetchall()
            finally:
                conn.close()

            shortcuts = []
            for row in rows:
                sync_guid = row[9] or ""
                is_default = False
                if default_guid:
                    if sync_guid == default_guid:
                        is_default = True
                    elif not sync_guid and default_engine_url and row[2] == default_engine_url:
                        # DB sync_guid is empty but URL matches the default engine in Preferences;
                        # adopt the known guid so it survives round-trip through the JSON.
                        sync_guid = default_guid
                        is_default = True
                elif default_engine_url and row[2] == default_engine_url:
                    # default_guid is empty in Preferences; match by URL only.
                    is_default = True
                shortcuts.append(
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
                        "sync_guid": sync_guid,
                        "safe_for_autoreplace": row[10] if row[10] is not None else 0,
                        "input_encodings": row[11] or "UTF-8",
                        "alternate_urls": row[12] or "[]",
                        "is_default": is_default,
                    }
                )

            shortcuts_json.write_text(json.dumps(shortcuts, indent=2), encoding="utf-8")
            self._report("search_shortcuts.json")
            self.logger.info(
                "Extracted %d user search shortcuts to %s", len(shortcuts), shortcuts_json
            )

        except sqlite3.Error as exc:
            self.logger.warning(
                "Failed to extract search shortcuts from %s: %s",
                web_data_src,
                exc,
            )

    def _restore_search_shortcuts(self, profile_path: Path, sync_folder_root: Path) -> None:
        """Restore search shortcuts from global JSON file to Web Data database (overwrite mode).

        Wipes all keyword rows (including built-ins) then inserts only user shortcuts from
        the JSON.  Chromium re-adds its built-ins on next launch, but to prevent it from
        also overriding default_search_provider.guid via choice-screen re-initialization,
        choice_screen_completion_program is set to a non-zero sentinel if not already
        present in Preferences.

        If any shortcut is flagged is_default, Preferences is updated so Chromium uses it
        as the default search engine after the next launch.
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

            prefs_path = profile_path / "Preferences"
            prefs: dict | None = None
            if prefs_path.exists():
                prefs = json.loads(prefs_path.read_text(encoding="utf-8"))

            # On Windows, Chromium verifies url_hash on startup and drops rows
            # whose hash doesn't match Pickle(id, url).  Load the OSCrypt key
            # once so we can compute a valid blob for every inserted row.
            aesgcm = self._load_oscrypt_key(profile_path.parent)
            if platform.system() == "Windows" and aesgcm is None:
                self.logger.warning(
                    "Cannot restore search shortcuts to %s: OSCrypt key unavailable "
                    "(launch the browser once to initialize Local State, then apply backup again)",
                    profile_path.name,
                )
                return

            conn = sqlite3.connect(str(web_data_dst))
            cursor = conn.cursor()

            cursor.execute("DELETE FROM keywords")

            # Predict the auto-increment ids so they match the url_hash computation.
            next_id = (
                cursor.execute("SELECT COALESCE(MAX(id), 0) FROM keywords").fetchone()[0] + 1
            )

            restored_default_guid: str | None = None
            default_shortcut: dict | None = None
            default_row_id: int | None = None

            for i, shortcut in enumerate(shortcuts):
                row_id = next_id + i
                sync_guid = shortcut.get("sync_guid") or ""
                if shortcut.get("is_default"):
                    if not sync_guid:
                        sync_guid = str(uuid.uuid4())
                    restored_default_guid = sync_guid
                    default_shortcut = shortcut
                    default_row_id = row_id

                url_hash_blob: bytes | None = None
                if aesgcm is not None:
                    url_hash_blob = self._make_url_hash(row_id, shortcut["url"], aesgcm)

                cursor.execute(
                    """
                    INSERT INTO keywords (
                        id, short_name, keyword, favicon_url, url, safe_for_autoreplace,
                        originating_url, date_created, usage_count, input_encodings,
                        suggest_url, prepopulate_id, created_by_policy, last_modified,
                        sync_guid, alternate_urls, image_url, search_url_post_params,
                        suggest_url_post_params, image_url_post_params, new_tab_url,
                        last_visited, created_from_play_api, is_active, starter_pack_id,
                        enforced_by_policy, featured_by_policy, url_hash
                    ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        row_id,
                        shortcut["short_name"],
                        shortcut["keyword"],
                        shortcut.get("favicon_url", ""),
                        shortcut["url"],
                        shortcut.get("safe_for_autoreplace", 0),
                        "",
                        shortcut.get("date_created", 0),
                        0,
                        shortcut.get("input_encodings", "UTF-8"),
                        shortcut.get("suggest_url", ""),
                        shortcut.get("prepopulate_id", 0),
                        0,
                        shortcut.get("last_modified", 0),
                        sync_guid,
                        shortcut.get("alternate_urls", "[]"),
                        "",
                        "",
                        "",
                        "",
                        "",
                        shortcut.get("last_modified", 0),
                        0,
                        shortcut.get("is_active", 1),
                        0,
                        0,
                        0,
                        url_hash_blob,
                    ),
                )

            # Sync the default-engine pointer in keywords_metadata so Chromium finds
            # our inserted engine when it validates the default on startup — otherwise
            # the stale ID causes Chromium to fall into its built-in repopulation path
            # and reset the default to DuckDuckGo / its own choice.
            # Also delete the encrypted backup blob: it anchors the old default and
            # Chromium will use it to overwrite our Preferences guid if it exists.
            if default_row_id is not None:
                try:
                    updated = cursor.execute(
                        "UPDATE keywords_metadata SET value = ? "
                        "WHERE key = 'Default Search Provider ID'",
                        (str(default_row_id),),
                    ).rowcount
                    if updated == 0:
                        cursor.execute(
                            "INSERT OR IGNORE INTO keywords_metadata (key, value) VALUES (?, ?)",
                            ("Default Search Provider ID", str(default_row_id)),
                        )
                    cursor.execute(
                        "DELETE FROM keywords_metadata "
                        "WHERE key = 'Default Search Provider Backup'",
                    )
                except sqlite3.OperationalError:
                    self.logger.debug("keywords_metadata unavailable — skipping ID sync")

            conn.commit()
            conn.close()

            # Point Preferences at the restored default engine so Chromium picks it up.
            # Also clear reset_occurred so Chromium doesn't override our choice on next launch.
            if restored_default_guid and prefs is not None:
                dsp = prefs.setdefault("default_search_provider", {})
                dsp["guid"] = restored_default_guid
                dsp.pop("reset_occurred", None)
                dsp.pop("reset_time", None)

                if default_shortcut is not None and default_row_id is not None:
                    alt_urls_raw = default_shortcut.get("alternate_urls", "[]")
                    try:
                        alt_urls = (
                            json.loads(alt_urls_raw)
                            if isinstance(alt_urls_raw, str)
                            else alt_urls_raw
                        )
                    except (json.JSONDecodeError, TypeError):
                        alt_urls = []
                    mirror = {
                        "alternate_urls": alt_urls,
                        "contextual_search_url": "",
                        "created_from_play_api": False,
                        "date_created": str(default_shortcut.get("date_created", 0)),
                        "favicon_url": default_shortcut.get("favicon_url", ""),
                        "id": str(default_row_id),
                        "image_search_branding_label": "",
                        "image_search_post_params": "",
                        "image_translate_source_language_param_key": "",
                        "image_translate_source_language_param_value": "",
                        "image_translate_target_language_param_key": "",
                        "image_url": "",
                        "image_url_post_params": "",
                        "is_active": default_shortcut.get("is_active", 1),
                        "keyword": default_shortcut.get("keyword", ""),
                        "last_modified": str(default_shortcut.get("last_modified", 0)),
                        "logo_url": "",
                        "new_tab_url": "",
                        "policy_origin": "",
                        "prepopulate_id": default_shortcut.get("prepopulate_id", 0),
                        "safe_for_autoreplace": bool(
                            default_shortcut.get("safe_for_autoreplace", False)
                        ),
                        "search_intent_params": [],
                        "short_name": default_shortcut.get("short_name", ""),
                        "side_image_search_param": "",
                        "suggestions_url": default_shortcut.get("suggest_url", ""),
                        "synced_guid": restored_default_guid,
                        "url": default_shortcut.get("url", ""),
                        "visual_url": "",
                    }
                    prefs.setdefault("default_search_provider_data", {})[
                        "mirrored_template_url_data"
                    ] = mirror

                prefs_path.write_text(json.dumps(prefs), encoding="utf-8")

            self._report("search shortcuts restored")
            self.logger.info("Restored %d search shortcuts from %s", len(shortcuts), shortcuts_json)

        except (sqlite3.Error, json.JSONDecodeError, KeyError) as exc:
            self.logger.warning("Failed to restore search shortcuts: %s", exc)

    def _sync_preferences_json(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        direction: str,
    ) -> None:
        """Extract/merge a curated subset of Preferences as preferences.json in the sync dir.

        Push: reads profile Preferences, writes only PREFERENCES_KEYS to preferences.json.
        Pull: reads preferences.json and deep-merges into profile Preferences, touching only
              keys that already exist there (never injects foreign keys).
        """
        prefs_path = profile_path / "Preferences"
        json_path = sync_profile_path / "preferences.json"

        local_mtime = prefs_path.stat().st_mtime if prefs_path.exists() else 0.0
        remote_mtime = json_path.stat().st_mtime if json_path.exists() else 0.0

        do_push = direction == "push" or (direction == "both" and local_mtime > remote_mtime)
        do_pull = direction == "pull" or (direction == "both" and remote_mtime > local_mtime)

        if do_push and prefs_path.exists():
            prefs = json.loads(prefs_path.read_bytes())
            extracted: dict = {}
            for dotted in PREFERENCES_KEYS:
                keys = dotted.split(".")
                found, value = _get_nested(prefs, keys)
                if found:
                    _set_nested(extracted, keys, value)
            json_path.parent.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(extracted), encoding="utf-8")
            # Remove legacy raw Preferences from sync dir (replaced by preferences.json)
            legacy = sync_profile_path / "Preferences"
            if legacy.exists():
                legacy.unlink()
            self._report("preferences.json")
            self._synced_count += 1
        elif do_pull and json_path.exists() and prefs_path.exists():
            saved = json.loads(json_path.read_bytes())
            prefs = json.loads(prefs_path.read_bytes())
            _merge_prefs(prefs, saved)
            prefs_path.write_bytes(json.dumps(prefs, separators=(",", ":")).encode())
            self._report("Preferences")
            self._synced_count += 1

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
            from src import config as _config

            excluded_ext_ids = set(_config.get_excluded_ext_settings_ids())
            self._sync_extensions(profile_path, sync_profile_path, direction)
            # Local Extension Settings: skip excluded IDs (they contain auto-regenerated cache).
            # Sync Extension Settings: always sync fully — it holds the user-configured subset
            # and is small by design (chrome.storage.sync quota is 100 KB).
            self._sync_leveldb_dir(
                profile_path, sync_profile_path, "Local Extension Settings", direction,
                name_filter=lambda n, ex=excluded_ext_ids: n not in ex,
            )
            self._sync_leveldb_dir(
                profile_path, sync_profile_path, "Sync Extension Settings", direction,
            )
            self._sync_leveldb_dir(
                profile_path,
                sync_profile_path,
                "IndexedDB",
                direction,
                name_filter=lambda n, ex=excluded_ext_ids: (
                    n.startswith("chrome-extension_")
                    and not any(n.startswith(f"chrome-extension_{e}_") for e in ex)
                ),
            )

        if dt.get("local_storage", True):
            self._sync_leveldb_dir(
                profile_path, sync_profile_path, "Local Storage/leveldb", direction
            )

        self._sync_preferences_json(profile_path, sync_profile_path, direction)

        plain_files: list[tuple[str, str | None]] = [
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
            if direction == "push":
                self._extract_search_shortcuts(profile_path, sync_profile_path)
            if direction in ("pull", "both"):
                self._restore_search_shortcuts(profile_path, sync_profile_path)

        self.logger.info("Profile sync complete: %s", profile_path.name)

    # ------------------------------------------------------------------
    # Top-level orchestration
    # ------------------------------------------------------------------

    def sync_all(
        self,
        only_browser: str | None = None,
        only_profile: str | None = None,
        force_direction: str | None = None,
    ) -> dict[str, bool]:
        """Sync installed, non-running browsers respecting saved config.

        *only_browser*: restrict sync to a single browser by name.
        *only_profile*: further restrict to a single profile directory name.
        *force_direction*: override per-profile direction ("push"/"pull"/"both").
          When "pull", uses restore_profile_from_backup for a clean overwrite.

        Returns a dict with 'is_first_sync' to indicate if this was initial setup.
        """
        from src import config as _config

        enabled_browsers = _config.get_enabled_browsers()
        enabled_profiles = _config.get_enabled_profiles()
        directions = _config.get_profile_directions()
        profiles_needing_restore = _config.get_profiles_needing_restore()
        data_types = DEFAULT_DATA_TYPES

        is_first_sync = not (self.sync_folder / "current.tar").exists()

        # Reset sync statistics
        self._synced_count = 0
        self._skipped_count = 0
        skipped_running: list[str] = []

        if is_first_sync:
            self.logger.info("Starting initial sync (first-time setup)")
        else:
            self.logger.info(
                "Starting sync_all (only_browser=%s, only_profile=%s, force_direction=%s)",
                only_browser, only_profile, force_direction,
            )

        browsers_to_sync = [
            b for b in self.browsers
            if only_browser is None or b.name == only_browser
        ]

        # Skip entirely if every targeted, non-disabled browser is running.
        manageable = [
            b for b in browsers_to_sync
            if enabled_browsers.get(b.name) is not False and b.is_installed()
        ]
        if manageable and all(b.is_running() for b in manageable):
            running_names = [b.name for b in manageable]
            self.logger.warning(
                "All targeted browsers running (%s) — skipping sync",
                ", ".join(running_names),
            )
            return {"is_first_sync": is_first_sync, "skipped_running": running_names}

        # Unpack current archive into a system temp directory so the cloud client
        # never sees individual profile files — only current.tar changes.
        current_archive = self.sync_folder / "current.tar"
        work_dir = Path(tempfile.mkdtemp(prefix="cps-work-"))

        success = False
        try:
            if current_archive.exists():
                self._report("Unpacking...")
                self._unpack_archive(current_archive, work_dir)

            # Prune excluded extension settings from the unpacked work dir so they
            # disappear from the next tar even if the old archive still contained them.
            excluded_ext_ids = _config.get_excluded_ext_settings_ids()
            for ext_id in excluded_ext_ids:
                stale = work_dir / "Local Extension Settings" / ext_id
                if stale.exists():
                    shutil.rmtree(stale)
                    self.logger.info("Pruned excluded ext settings: %s", ext_id)
                indexed_db = work_dir / "IndexedDB"
                if indexed_db.exists():
                    for entry in indexed_db.iterdir():
                        if entry.is_dir() and entry.name.startswith(f"chrome-extension_{ext_id}_"):
                            shutil.rmtree(entry)
                            self.logger.info("Pruned excluded ext IndexedDB: %s", entry.name)

            for browser in browsers_to_sync:
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
                    skipped_running.append(browser.name)
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
                if only_profile:
                    profiles = [p for p in profiles if p.name == only_profile]
                if not profiles:
                    self.logger.info("Browser %s: no matching profiles — skipping", browser.name)
                    continue

                self.logger.info("Browser %s: syncing %d profile(s)", browser.name, len(profiles))
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
                        if needs_restore:
                            self.logger.info(
                                "Profile %s/%s: performing complete restore from backup",
                                browser.name,
                                profile_path.name,
                            )
                            self.restore_profile_from_backup(
                                profile_path, work_dir, data_types,
                                browser=browser,
                                on_progress=self._progress_cb,
                            )
                            self._install_external_extensions(work_dir, browser)
                            if force_direction != "pull":
                                _config.clear_restore_flag(browser.name, profile_path.name)
                        else:
                            self.sync_browser_profile(
                                profile_path, work_dir, data_types,
                                direction=direction,
                                on_progress=self._progress_cb,
                            )
                            if direction in ("pull", "both"):
                                self._install_external_extensions(work_dir, browser)
                    except OSError:
                        self.logger.exception(
                            "Failed to sync profile %s for browser %s",
                            profile_path.name,
                            browser.name,
                        )
                        if needs_restore and force_direction != "pull":
                            raise
            success = True
        finally:
            # Repack only when sync completed and something actually changed.
            # Skipping when synced_count == 0 prevents the file-watcher from seeing
            # an updated archive mtime and immediately triggering another no-op sync.
            if success and self._synced_count > 0 and any(work_dir.iterdir()):
                if self._validate_archive_content(work_dir):
                    self._report("Packing...")
                    self._pack_to_archive(work_dir, current_archive)
            shutil.rmtree(work_dir)

        # Log summary
        summary = (
            f"Synced: {self._synced_count} items, "
            f"Skipped: {self._skipped_count} items (unchanged)"
        )
        if is_first_sync:
            self.logger.info("Initial sync complete — %s", summary)
        else:
            self.logger.info("Sync complete — %s", summary)

        return {"is_first_sync": is_first_sync, "skipped_running": skipped_running}

_clean_logger = logging.getLogger(__name__ + ".clean")


def clean_external_extensions(browsers: list) -> None:
    """Remove Web Store extension registrations created by this app for all browsers.

    Registry-based browsers (Windows): enumerates and deletes all HKCU extension keys.
    File-based browsers (macOS/Linux or no registry key): removes stub files.
    """
    on_windows = platform.system() == "Windows"
    for browser in browsers:
        reg_key = browser.windows_extensions_registry_key() if on_windows else None
        if reg_key:
            _wipe_registry_extensions(reg_key)
            force_key = browser.windows_force_list_registry_key() if on_windows else None
            if force_key:
                _wipe_registry_key(force_key)
        else:
            ext_dir = browser.external_extensions_dir()
            if ext_dir and ext_dir.exists():
                for stub in ext_dir.glob("*.json"):
                    stub.unlink(missing_ok=True)
                    _clean_logger.info("Removed extension stub: %s", stub.stem)


def _wipe_registry_extensions(reg_subkey: str) -> None:
    """Delete all HKCU extension sub-keys under reg_subkey."""
    import winreg  # Windows-only stdlib module

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_subkey) as base:
            ext_ids = []
            i = 0
            while True:
                try:
                    ext_ids.append(winreg.EnumKey(base, i))
                    i += 1
                except OSError:
                    break
    except FileNotFoundError:
        return

    for ext_id in ext_ids:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_subkey + "\\" + ext_id)
            _clean_logger.info("Removed registry entry: %s", ext_id)
        except OSError:
            _clean_logger.warning("Failed to remove registry entry: %s", ext_id)


def _wipe_registry_key(reg_subkey: str) -> None:
    """Delete all values from an HKCU registry key (used for policy list keys)."""
    import winreg  # Windows-only stdlib module

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, reg_subkey, access=winreg.KEY_ALL_ACCESS
        ) as key:
            value_names = []
            i = 0
            while True:
                try:
                    value_names.append(winreg.EnumValue(key, i)[0])
                    i += 1
                except OSError:
                    break
            for name in value_names:
                winreg.DeleteValue(key, name)
        _clean_logger.info("Cleared registry key: %s", reg_subkey)
    except FileNotFoundError:
        pass
    except OSError:
        _clean_logger.warning("Failed to clear registry key: %s", reg_subkey)
