from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from src.sync import archive as _archive
from src.sync import extensions as _extensions
from src.sync import leveldb as _leveldb
from src.sync import prefs as _prefs
from src.sync import shortcuts as _shortcuts

if TYPE_CHECKING:
    from src.browsers.base import BrowserBase


def _find_rclone_fallback_paths() -> list[Path]:
    import sys
    if sys.platform == "darwin":
        return [Path("/opt/homebrew/bin/rclone"), Path("/usr/local/bin/rclone")]
    elif sys.platform == "win32":
        return [
            Path("C:/Program Files/rclone/rclone.exe"),
            Path("C:/Program Files (x86)/rclone/rclone.exe"),
            Path.home() / "AppData" / "Local" / "rclone" / "rclone.exe",
        ]
    return [Path("/usr/bin/rclone"), Path("/usr/local/bin/rclone")]


_FALLBACK_PATHS = _find_rclone_fallback_paths()


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

_RCLONE_PROGRESS_RE = re.compile(
    r"Transferred:\s+[\d.]+\s*\w+\s*/\s*[\d.]+\s*\w+,\s*(\d+)%"
)

_LOG = logging.getLogger(__name__)

clean_external_extensions = _extensions.clean_external_extensions


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
        self._synced_count: int = 0
        self._skipped_count: int = 0

    def _report(self, description: str) -> None:
        if self._progress_cb:
            self._progress_cb(description)

    def _run_rclone(self, cmd: list[str], description: str = "") -> None:
        self._report(f"{description} (starting...)" if description else "Starting sync...")
        self.logger.debug("Executing: %s", " ".join(cmd))
        output_lines: list[str] = []
        try:
            process = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            for line in iter(process.stdout.readline, ""):
                line = line.strip()
                if not line:
                    continue
                output_lines.append(line)
                match = _RCLONE_PROGRESS_RE.search(line)
                if match:
                    pct = match.group(1)
                    self._report(f"{description} ({pct}%)" if description else f"Syncing ({pct}%)")
                elif "Transferred:" in line:
                    self._report(f"{description}..." if description else "Syncing...")
            process.wait()
            if process.returncode != 0:
                error_output = "\n".join(output_lines[-10:]) if output_lines else "No output"
                self.logger.error("rclone failed:\n%s", error_output)
                raise subprocess.CalledProcessError(process.returncode, cmd, output=error_output)
            self.logger.debug("rclone complete: %s", description or cmd[1])
        except subprocess.CalledProcessError as exc:
            self.logger.exception("rclone failed")
            raise OSError(
                f"rclone sync failed: {exc.output}" if exc.output else f"rclone sync failed: {exc}"
            ) from exc
        except FileNotFoundError as exc:
            self.logger.exception("rclone not found")
            raise OSError(f"rclone not found: {exc}") from exc

    def _rclone_sync(self, src: Path, dst: Path, description: str = "") -> None:
        cmd = [
            str(find_rclone() or "rclone"), "sync",
            str(src), str(dst),
            "--stats", "1s",
            "--stats-one-line",
            "--transfers", "8",
            "--checkers", "16",
            "--exclude", "._*",
            "--checksum",
            "--fast-list",
        ]
        self._run_rclone(cmd, description)
        self.logger.debug("rclone sync complete: %s → %s", src, dst)

    def _sync_file(self, src: Path, dst: Path, direction: str = "both") -> None:
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
        else:
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
        self.logger.info("Syncing profile: %s ↔ %s", profile_path, sync_profile_path)

        if dt.get("extensions", True):
            from src import config as _config

            excluded_ext_ids = set(_config.get_excluded_ext_settings_ids())
            s, sk = _extensions.sync_extensions(
                profile_path, sync_profile_path, direction, self._report
            )
            self._synced_count += s
            self._skipped_count += sk

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

        self._synced_count += _prefs.sync_preferences_json(
            profile_path, sync_profile_path, direction, self._report
        )

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
                _shortcuts.extract_search_shortcuts(profile_path, sync_profile_path, self._report)
            if direction in ("pull", "both"):
                _shortcuts.restore_search_shortcuts(profile_path, sync_profile_path, self._report)

        self.logger.info("Profile sync complete: %s", profile_path.name)

    def restore_profile_from_backup(
        self,
        profile_path: Path,
        sync_profile_path: Path,
        data_types: dict[str, bool] | None = None,
        *,
        browser: object | None = None,
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        from src import config as _config

        self._progress_cb = on_progress
        dt = data_types or {}

        if not sync_profile_path.exists():
            self.logger.warning("Backup does not exist at %s — cannot restore", sync_profile_path)
            return

        self.logger.info("Restoring profile from backup: %s → %s", sync_profile_path, profile_path)

        items_to_delete = []
        if dt.get("extensions", True):
            items_to_delete.extend([
                profile_path / "Extensions",
                profile_path / "Local Extension Settings",
                profile_path / "Sync Extension Settings",
                profile_path / "Extension State",
                profile_path / "Extension Rules",
            ])
            indexed_db = profile_path / "IndexedDB"
            if indexed_db.exists():
                items_to_delete.extend(
                    e for e in indexed_db.iterdir()
                    if e.is_dir() and e.name.startswith("chrome-extension_")
                )

        if dt.get("local_storage", True):
            items_to_delete.append(profile_path / "Local Storage")
        if dt.get("bookmarks", True):
            items_to_delete.append(profile_path / "Bookmarks")
        if dt.get("custom_dictionary", True):
            items_to_delete.append(profile_path / "Custom Dictionary.txt")

        for item in items_to_delete:
            if item.exists():
                if item.is_dir():
                    self.logger.debug("Deleting directory: %s", item)
                    shutil.rmtree(item)
                else:
                    self.logger.debug("Deleting file: %s", item)
                    item.unlink()
                self._synced_count += 1

        is_ungoogled = getattr(browser, "ungoogled", True)
        excluded_ext_ids: list[str] = (
            [] if is_ungoogled else _config.get_ungoogled_only_extensions()
        )

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

        self._run_rclone(cmd, "Restoring from backup")

        json_path = sync_profile_path / "preferences.json"
        prefs_path = profile_path / "Preferences"
        if json_path.exists() and prefs_path.exists():
            saved = json.loads(json_path.read_bytes())
            prefs = json.loads(prefs_path.read_bytes())
            if dt.get("extensions", True):
                prefs.get("extensions", {}).pop("settings", None)
            _prefs.merge_prefs(prefs, saved)
            prefs_path.write_bytes(json.dumps(prefs, separators=(",", ":")).encode())
            self.logger.info("Merged preferences.json into %s", prefs_path)

        if dt.get("search_shortcuts", True):
            _shortcuts.restore_search_shortcuts(profile_path, sync_profile_path, self._report)

        self.logger.info("Profile restore complete: %s", profile_path.name)

    def sync_all(
        self,
        only_browser: str | None = None,
        only_profile: str | None = None,
        force_direction: str | None = None,
    ) -> dict[str, bool]:
        from src import config as _config

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

        current_archive = self.sync_folder / "current.tar"
        work_dir = Path(tempfile.mkdtemp(prefix="cps-work-"))

        ungoogled_only_ext_ids = _config.get_ungoogled_only_extensions()
        excluded_ext_ids = _config.get_excluded_ext_settings_ids()

        success = False
        try:
            if current_archive.exists():
                self._report("Unpacking...")
                _archive.unpack_archive(current_archive, work_dir)

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
                        "Browser %s is running — skipping to avoid data corruption", browser.name,
                    )
                    skipped_running.append(browser.name)
                    continue

                profiles = browser.discover_profiles()
                if not profiles:
                    self.logger.info("Browser %s: no profiles found", browser.name)
                    continue

                allowed = enabled_profiles.get(browser.name)
                if not allowed:
                    self.logger.info(
                        "Browser %s: no enabled profiles in config — skipping", browser.name
                    )
                    continue

                profiles = [p for p in profiles if p.name in allowed]
                if only_profile:
                    profiles = [p for p in profiles if p.name == only_profile]
                if not profiles:
                    self.logger.info(
                        "Browser %s: no matching profiles — skipping", browser.name
                    )
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
                                browser.name, profile_path.name,
                            )
                            self.restore_profile_from_backup(
                                profile_path, work_dir, data_types,
                                browser=browser, on_progress=self._progress_cb,
                            )
                            _extensions.install_external_extensions(
                                work_dir, browser,
                                ungoogled_only_ext_ids=ungoogled_only_ext_ids,
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
                                )
                    except OSError:
                        self.logger.exception(
                            "Failed to sync profile %s for browser %s",
                            profile_path.name, browser.name,
                        )
                        if needs_restore and force_direction != "pull":
                            raise
            success = True
        finally:
            if success and self._synced_count > 0 and any(work_dir.iterdir()):
                if _archive.validate_archive_content(work_dir):
                    self._report("Packing...")
                    _archive.pack_to_archive(work_dir, current_archive)
            shutil.rmtree(work_dir)

        summary = (
            f"Synced: {self._synced_count} items, "
            f"Skipped: {self._skipped_count} items (unchanged)"
        )
        if is_first_sync:
            self.logger.info("Initial sync complete — %s", summary)
        else:
            self.logger.info("Sync complete — %s", summary)

        return {"is_first_sync": is_first_sync, "skipped_running": skipped_running}
