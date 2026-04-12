from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.sync_engine import NEVER_SYNC, SyncEngine, _parse_version, find_rclone

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine(sync_folder: Path, browsers: list | None = None) -> SyncEngine:
    return SyncEngine(sync_folder=sync_folder, browsers=browsers or [])


def _make_browser(
    name: str = "TestBrowser",
    installed: bool = True,
    running: bool = False,
    profiles: list[Path] | None = None,
) -> MagicMock:
    mock = MagicMock()
    mock.name = name
    mock.is_installed.return_value = installed
    mock.is_running.return_value = running
    mock.discover_profiles.return_value = profiles or []
    mock.external_extensions_dir.return_value = None
    return mock


def _write_file(path: Path, content: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------


def test_parse_version_standard() -> None:
    assert _parse_version("1.2.3") == (1, 2, 3)


def test_parse_version_single() -> None:
    assert _parse_version("5") == (5,)


def test_parse_version_malformed() -> None:
    assert _parse_version("abc") == (0,)


def test_parse_version_empty() -> None:
    assert _parse_version("") == (0,)


def test_parse_version_mixed_malformed() -> None:
    assert _parse_version("1.abc.3") == (0,)


# ---------------------------------------------------------------------------
# _copy_leveldb_atomic
# ---------------------------------------------------------------------------


def test_copy_leveldb_atomic_success(tmp_path: Path) -> None:
    src = tmp_path / "src_db"
    src.mkdir()
    (src / "MANIFEST").write_text("data")
    (src / "000001.ldb").write_text("ldb")

    dst = tmp_path / "dst_db"
    engine = _make_engine(tmp_path)
    engine._copy_leveldb_atomic(src, dst)

    assert dst.exists()
    assert (dst / "MANIFEST").read_text() == "data"
    assert (dst / "000001.ldb").read_text() == "ldb"
    assert not (tmp_path / "dst_db.tmp").exists()


def test_copy_leveldb_atomic_failure_dst_untouched(tmp_path: Path) -> None:
    src = tmp_path / "src_db"
    src.mkdir()
    (src / "file.txt").write_text("new")

    dst = tmp_path / "dst_db"
    dst.mkdir()
    (dst / "original.txt").write_text("original")

    engine = _make_engine(tmp_path)
    with patch("shutil.copytree", side_effect=OSError("disk full")):
        engine._copy_leveldb_atomic(src, dst)

    # dst must be untouched
    assert (dst / "original.txt").read_text() == "original"
    assert not (dst / "file.txt").exists()


def test_copy_leveldb_atomic_tmp_cleaned_before_retry(tmp_path: Path) -> None:
    src = tmp_path / "src_db"
    src.mkdir()
    (src / "a.txt").write_text("a")

    dst = tmp_path / "dst_db"
    # Simulate a leftover .tmp from a previous failed attempt
    tmp_dir = tmp_path / "dst_db.tmp"
    tmp_dir.mkdir()
    (tmp_dir / "stale.txt").write_text("stale")

    engine = _make_engine(tmp_path)
    engine._copy_leveldb_atomic(src, dst)

    assert dst.exists()
    assert (dst / "a.txt").read_text() == "a"
    assert not tmp_dir.exists()


# ---------------------------------------------------------------------------
# _rotate_backups
# ---------------------------------------------------------------------------


def test_rotate_backups_full_rotation(tmp_path: Path) -> None:
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()

    current = sync_folder / "current"
    backup1 = sync_folder / "backup-1"
    backup2 = sync_folder / "backup-2"

    current.mkdir()
    (current / "profile.json").write_text("current")
    backup1.mkdir()
    (backup1 / "profile.json").write_text("backup1")
    backup2.mkdir()
    (backup2 / "profile.json").write_text("backup2")

    engine = _make_engine(sync_folder)
    engine._rotate_backups()

    assert not backup2.exists() or (backup2 / "profile.json").read_text() == "backup1"
    new_backup2 = sync_folder / "backup-2"
    assert new_backup2.exists()
    assert (new_backup2 / "profile.json").read_text() == "backup1"

    new_backup1 = sync_folder / "backup-1"
    assert new_backup1.exists()
    assert (new_backup1 / "profile.json").read_text() == "current"


def test_rotate_backups_first_run_no_existing_backups(tmp_path: Path) -> None:
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    current = sync_folder / "current"
    current.mkdir()
    (current / "data.txt").write_text("init")

    engine = _make_engine(sync_folder)
    engine._rotate_backups()

    backup1 = sync_folder / "backup-1"
    assert backup1.exists()
    assert (backup1 / "data.txt").read_text() == "init"
    assert not (sync_folder / "backup-2").exists()


def test_rotate_backups_no_current(tmp_path: Path) -> None:
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    # No current, no backups — should not raise
    engine = _make_engine(sync_folder)
    engine._rotate_backups()
    assert not (sync_folder / "backup-1").exists()


# ---------------------------------------------------------------------------
# _sync_file
# ---------------------------------------------------------------------------


def test_sync_file_src_newer_copies_to_dst(tmp_path: Path) -> None:
    src = tmp_path / "src" / "Bookmarks"
    dst = tmp_path / "dst" / "Bookmarks"
    _write_file(src, "profile", mtime=2000.0)
    _write_file(dst, "sync", mtime=1000.0)

    engine = _make_engine(tmp_path)
    engine._sync_file(src, dst)

    assert dst.read_text() == "profile"


def test_sync_file_dst_newer_copies_to_src(tmp_path: Path) -> None:
    src = tmp_path / "src" / "Bookmarks"
    dst = tmp_path / "dst" / "Bookmarks"
    _write_file(src, "profile", mtime=1000.0)
    _write_file(dst, "sync", mtime=2000.0)

    engine = _make_engine(tmp_path)
    engine._sync_file(src, dst)

    assert src.read_text() == "sync"


def test_sync_file_equal_mtime_no_copy(tmp_path: Path) -> None:
    src = tmp_path / "src" / "Bookmarks"
    dst = tmp_path / "dst" / "Bookmarks"
    _write_file(src, "profile", mtime=1500.0)
    _write_file(dst, "sync", mtime=1500.0)

    engine = _make_engine(tmp_path)
    engine._sync_file(src, dst)

    # Neither file should be overwritten
    assert src.read_text() == "profile"
    assert dst.read_text() == "sync"


def test_sync_file_missing_dst_creates_it(tmp_path: Path) -> None:
    src = tmp_path / "src" / "Bookmarks"
    dst = tmp_path / "deep" / "nested" / "Bookmarks"
    _write_file(src, "hello", mtime=1000.0)

    engine = _make_engine(tmp_path)
    engine._sync_file(src, dst)

    assert dst.exists()
    assert dst.read_text() == "hello"


def test_sync_file_push_only_copies_profile_to_sync(tmp_path: Path) -> None:
    src = tmp_path / "src" / "Bookmarks"
    dst = tmp_path / "dst" / "Bookmarks"
    _write_file(src, "profile", mtime=2000.0)
    _write_file(dst, "sync", mtime=1000.0)

    engine = _make_engine(tmp_path)
    engine._sync_file(src, dst, direction="push")

    assert dst.read_text() == "profile"
    assert src.read_text() == "profile"


def test_sync_file_pull_only_copies_sync_to_profile(tmp_path: Path) -> None:
    src = tmp_path / "src" / "Bookmarks"
    dst = tmp_path / "dst" / "Bookmarks"
    _write_file(src, "profile", mtime=1000.0)
    _write_file(dst, "sync", mtime=2000.0)

    engine = _make_engine(tmp_path)
    engine._sync_file(src, dst, direction="pull")

    assert src.read_text() == "sync"
    assert dst.read_text() == "sync"


def test_sync_file_push_skips_when_sync_is_newer(tmp_path: Path) -> None:
    src = tmp_path / "src" / "Bookmarks"
    dst = tmp_path / "dst" / "Bookmarks"
    _write_file(src, "profile", mtime=1000.0)
    _write_file(dst, "sync", mtime=2000.0)

    engine = _make_engine(tmp_path)
    engine._sync_file(src, dst, direction="push")

    # sync is newer but direction is push — no copy should happen
    assert dst.read_text() == "sync"
    assert src.read_text() == "profile"


def test_sync_file_pull_skips_when_profile_is_newer(tmp_path: Path) -> None:
    src = tmp_path / "src" / "Bookmarks"
    dst = tmp_path / "dst" / "Bookmarks"
    _write_file(src, "profile", mtime=2000.0)
    _write_file(dst, "sync", mtime=1000.0)

    engine = _make_engine(tmp_path)
    engine._sync_file(src, dst, direction="pull")

    # profile is newer but direction is pull — no copy should happen
    assert src.read_text() == "profile"
    assert dst.read_text() == "sync"


def test_sync_all_uses_direction_from_config(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "Default"
    profile.mkdir(parents=True)
    _write_file(profile / "Bookmarks", "profile-data", mtime=2000.0)
    _write_file(profile / "Preferences", "{}", mtime=2000.0)

    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()

    # Pre-populate sync with older data
    sync_bk = sync_folder / "current" / "TestBrowser" / "Default" / "Bookmarks"
    _write_file(sync_bk, "sync-data", mtime=1000.0)
    sync_prefs = sync_folder / "current" / "TestBrowser" / "Default" / "Preferences"
    _write_file(sync_prefs, "{}", mtime=1000.0)

    browser = _make_browser(
        name="TestBrowser", installed=True, running=False, profiles=[profile]
    )
    engine = _make_engine(sync_folder, browsers=[browser])

    directions = {"TestBrowser": {"Default": "push"}}
    with patch("src.config.get_enabled_browsers", return_value={"TestBrowser": True}), \
         patch("src.config.get_enabled_profiles", return_value={"TestBrowser": ["Default"]}), \
         patch("src.config.get_profile_directions", return_value=directions):
        engine.sync_all()

    # Push: profile (newer) → sync should be updated
    assert sync_bk.read_text() == "profile-data"
    # Profile itself must not be overwritten (push skips reverse copy)
    assert (profile / "Bookmarks").read_text() == "profile-data"


# ---------------------------------------------------------------------------
# _sync_extensions (version-based)
# ---------------------------------------------------------------------------


def _make_ext_version_dir(
    base: Path,
    ext_id: str,
    version: str,
    manifest_version: str | None = None,
    *,
    webstore: bool = False,
) -> Path:
    """Create Extensions/<ext_id>/<version>/ with optional manifest.json.

    If webstore=True, creates _metadata/verified_contents.json to simulate
    a Web Store extension. Default is False (unpacked/developer extension).
    """
    ver_dir = base / "Extensions" / ext_id / version
    ver_dir.mkdir(parents=True, exist_ok=True)
    if manifest_version is not None:
        (ver_dir / "manifest.json").write_text(
            json.dumps({"version": manifest_version}), encoding="utf-8"
        )
    if webstore:
        # Create verified_contents.json to simulate Web Store extension
        metadata_dir = ver_dir / "_metadata"
        metadata_dir.mkdir(parents=True, exist_ok=True)
        (metadata_dir / "verified_contents.json").write_text(
            json.dumps([{"description": "test"}]), encoding="utf-8"
        )
    return ver_dir


def test_sync_extensions_profile_version_wins(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    _make_ext_version_dir(profile, "abc123", "1.5.0", "1.5.0")
    _make_ext_version_dir(sync, "abc123", "1.0.0", "1.0.0")

    engine = _make_engine(tmp_path)
    engine._sync_extensions(profile, sync)

    # sync should now have 1.5.0
    assert (sync / "Extensions" / "abc123" / "1.5.0").exists()


def test_sync_extensions_sync_version_wins(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    _make_ext_version_dir(profile, "abc123", "1.0.0", "1.0.0")
    _make_ext_version_dir(sync, "abc123", "2.0.0", "2.0.0")

    engine = _make_engine(tmp_path)
    engine._sync_extensions(profile, sync)

    # profile should now have 2.0.0
    assert (profile / "Extensions" / "abc123" / "2.0.0").exists()


def test_sync_extensions_equal_versions_no_copy(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    _make_ext_version_dir(profile, "abc123", "1.0.0", "1.0.0")
    _make_ext_version_dir(sync, "abc123", "1.0.0", "1.0.0")

    engine = _make_engine(tmp_path)
    # Should not raise and should result in no additional dirs created
    engine._sync_extensions(profile, sync)

    profile_subdirs = list((profile / "Extensions" / "abc123").iterdir())
    sync_subdirs = list((sync / "Extensions" / "abc123").iterdir())
    assert len(profile_subdirs) == 1
    assert len(sync_subdirs) == 1


def test_sync_extensions_missing_manifest_fallback_to_dirname(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    # No manifest.json — version parsed from dir name
    _make_ext_version_dir(profile, "extxyz", "2.0.0")  # no manifest
    _make_ext_version_dir(sync, "extxyz", "1.0.0")  # no manifest

    engine = _make_engine(tmp_path)
    engine._sync_extensions(profile, sync)

    # profile 2.0.0 > sync 1.0.0 → copied to sync
    assert (sync / "Extensions" / "extxyz" / "2.0.0").exists()


def test_sync_extensions_version_dir_with_0_suffix(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    # Chromium _0 suffix stripped
    _make_ext_version_dir(profile, "extabc", "1.5.0_0")  # effectively 1.5.0
    _make_ext_version_dir(sync, "extabc", "1.4.0_0")  # effectively 1.4.0

    engine = _make_engine(tmp_path)
    engine._sync_extensions(profile, sync)

    # profile 1.5.0 > sync 1.4.0 → sync gets 1.5.0_0
    assert (sync / "Extensions" / "extabc" / "1.5.0_0").exists()


def test_sync_extensions_only_in_sync_copies_to_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    # Extension only in sync — should be copied to profile
    _make_ext_version_dir(sync, "newext", "3.0.0", "3.0.0")

    engine = _make_engine(tmp_path)
    engine._sync_extensions(profile, sync)

    assert (profile / "Extensions" / "newext" / "3.0.0").exists()


# ---------------------------------------------------------------------------
# _sync_leveldb_dir (mtime-based)
# ---------------------------------------------------------------------------


def _make_leveldb_unit(base: Path, subpath: str, name: str, content: str, mtime: float) -> Path:
    unit = base / subpath / name
    unit.mkdir(parents=True, exist_ok=True)
    f = unit / "data.ldb"
    f.write_text(content)
    os.utime(f, (mtime, mtime))
    return unit


def test_sync_leveldb_dir_profile_newer_copies_to_sync(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    _make_leveldb_unit(profile, "Local Extension Settings", "unit1", "profile-data", 2000.0)
    _make_leveldb_unit(sync, "Local Extension Settings", "unit1", "sync-data", 1000.0)

    engine = _make_engine(tmp_path)
    engine._sync_leveldb_dir(profile, sync, "Local Extension Settings")

    assert (sync / "Local Extension Settings" / "unit1" / "data.ldb").read_text() == "profile-data"


def test_sync_leveldb_dir_sync_newer_copies_to_profile(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    _make_leveldb_unit(profile, "Local Extension Settings", "unit1", "profile-data", 1000.0)
    _make_leveldb_unit(sync, "Local Extension Settings", "unit1", "sync-data", 2000.0)

    engine = _make_engine(tmp_path)
    engine._sync_leveldb_dir(profile, sync, "Local Extension Settings")

    assert (profile / "Local Extension Settings" / "unit1" / "data.ldb").read_text() == "sync-data"


def test_sync_leveldb_dir_equal_mtime_no_copy(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    _make_leveldb_unit(profile, "Sync Extension Settings", "unit1", "p", 1500.0)
    _make_leveldb_unit(sync, "Sync Extension Settings", "unit1", "s", 1500.0)

    engine = _make_engine(tmp_path)
    engine._sync_leveldb_dir(profile, sync, "Sync Extension Settings")

    # No overwrite — each side retains original content
    assert (profile / "Sync Extension Settings" / "unit1" / "data.ldb").read_text() == "p"
    assert (sync / "Sync Extension Settings" / "unit1" / "data.ldb").read_text() == "s"


def test_sync_leveldb_dir_empty_dir(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync = tmp_path / "sync"
    # Empty LevelDB dir (no files) — mtime is 0.0, no copy expected
    unit = profile / "Local Extension Settings" / "empty_unit"
    unit.mkdir(parents=True, exist_ok=True)

    engine = _make_engine(tmp_path)
    # Should not raise
    engine._sync_leveldb_dir(profile, sync, "Local Extension Settings")


# ---------------------------------------------------------------------------
# sync_browser_profile
# ---------------------------------------------------------------------------


def test_sync_browser_profile_syncs_plain_files(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync_profile = tmp_path / "sync_profile"
    profile.mkdir()

    _write_file(profile / "Bookmarks", '{"roots":{}}', mtime=2000.0)
    _write_file(profile / "Preferences", '{"settings":{}}', mtime=2000.0)
    _write_file(profile / "Custom Dictionary.txt", "word1\nword2", mtime=2000.0)

    engine = _make_engine(tmp_path)
    engine.sync_browser_profile(profile, sync_profile)

    assert (sync_profile / "Bookmarks").read_text() == '{"roots":{}}'
    assert (sync_profile / "Preferences").read_text() == '{"settings":{}}'
    assert (sync_profile / "Custom Dictionary.txt").read_text() == "word1\nword2"


def test_sync_browser_profile_syncs_extensions(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync_profile = tmp_path / "sync_profile"
    _make_ext_version_dir(profile, "testext", "1.0.0", "1.0.0")

    engine = _make_engine(tmp_path)
    engine.sync_browser_profile(profile, sync_profile)

    assert (sync_profile / "Extensions" / "testext" / "1.0.0").exists()


def test_sync_browser_profile_empty_profile_dir(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync_profile = tmp_path / "sync_profile"
    profile.mkdir()

    engine = _make_engine(tmp_path)
    # Should not raise on empty profile
    engine.sync_browser_profile(profile, sync_profile)
    assert sync_profile.exists()


def test_sync_browser_profile_data_types_extensions_disabled(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync_profile = tmp_path / "sync_profile"
    _make_ext_version_dir(profile, "testext", "1.0.0", "1.0.0")
    _write_file(profile / "Bookmarks", "bk", mtime=2000.0)

    engine = _make_engine(tmp_path)
    engine.sync_browser_profile(profile, sync_profile, {"extensions": False, "bookmarks": True})

    assert not (sync_profile / "Extensions").exists()
    assert (sync_profile / "Bookmarks").exists()


def test_sync_browser_profile_data_types_bookmarks_disabled(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync_profile = tmp_path / "sync_profile"
    profile.mkdir()
    _write_file(profile / "Bookmarks", "bk", mtime=2000.0)
    _write_file(profile / "Preferences", "{}", mtime=2000.0)

    engine = _make_engine(tmp_path)
    engine.sync_browser_profile(profile, sync_profile, {"bookmarks": False})

    assert not (sync_profile / "Bookmarks").exists()
    assert (sync_profile / "Preferences").exists()  # always synced


def test_sync_browser_profile_preferences_always_synced(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync_profile = tmp_path / "sync_profile"
    profile.mkdir()
    _write_file(profile / "Preferences", "{}", mtime=2000.0)

    engine = _make_engine(tmp_path)
    # Even with everything disabled, Preferences must be synced
    engine.sync_browser_profile(
        profile, sync_profile,
        {"extensions": False, "bookmarks": False, "custom_dictionary": False,
         "local_storage": False, "indexeddb": False},
    )

    assert (sync_profile / "Preferences").exists()


# ---------------------------------------------------------------------------
# sync_all
# ---------------------------------------------------------------------------


def test_sync_all_skips_running_browser(tmp_path: Path) -> None:
    browser = _make_browser(installed=True, running=True)
    engine = _make_engine(tmp_path, browsers=[browser])

    engine.sync_all()

    browser.discover_profiles.assert_not_called()


def test_sync_all_skips_uninstalled_browser(tmp_path: Path) -> None:
    browser = _make_browser(installed=False, running=False)
    engine = _make_engine(tmp_path, browsers=[browser])

    engine.sync_all()

    browser.discover_profiles.assert_not_called()


def test_sync_all_processes_installed_idle_browser(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "Default"
    profile.mkdir(parents=True)
    _write_file(profile / "Bookmarks", '{"roots":{}}', mtime=2000.0)
    _write_file(profile / "Preferences", "{}", mtime=2000.0)

    browser = _make_browser(
        name="TestBrowser", installed=True, running=False, profiles=[profile]
    )
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()

    engine = _make_engine(sync_folder, browsers=[browser])

    with patch("src.config.get_enabled_browsers", return_value={"TestBrowser": True}), \
         patch("src.config.get_enabled_profiles", return_value={"TestBrowser": ["Default"]}), \
         patch("src.config.get_profile_directions", return_value={}):
        engine.sync_all()

    synced = sync_folder / "current" / "TestBrowser" / "Default" / "Bookmarks"
    assert synced.exists()
    assert synced.read_text() == '{"roots":{}}'


def test_sync_all_no_profiles_found(tmp_path: Path) -> None:
    browser = _make_browser(installed=True, running=False, profiles=[])
    engine = _make_engine(tmp_path, browsers=[browser])
    # Should not raise
    engine.sync_all()


def test_sync_all_skips_disabled_browser(tmp_path: Path) -> None:
    profile = tmp_path / "Default"
    profile.mkdir()
    browser = _make_browser(name="MyBrowser", installed=True, running=False, profiles=[profile])
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    engine = _make_engine(sync_folder, browsers=[browser])

    with patch("src.config.get_enabled_browsers", return_value={"MyBrowser": False}):
        engine.sync_all()

    browser.discover_profiles.assert_not_called()


def test_sync_all_filters_profiles(tmp_path: Path) -> None:
    default = tmp_path / "profiles" / "Default"
    profile1 = tmp_path / "profiles" / "Profile 1"
    default.mkdir(parents=True)
    profile1.mkdir(parents=True)
    _write_file(default / "Preferences", "{}", mtime=1000.0)
    _write_file(profile1 / "Preferences", "{}", mtime=1000.0)

    browser = _make_browser(
        name="TB", installed=True, running=False, profiles=[default, profile1]
    )
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    engine = _make_engine(sync_folder, browsers=[browser])

    with patch("src.config.get_enabled_profiles", return_value={"TB": ["Default"]}):
        engine.sync_all()

    assert (sync_folder / "current" / "TB" / "Default").exists()
    assert not (sync_folder / "current" / "TB" / "Profile 1").exists()


# ---------------------------------------------------------------------------
# update_metadata
# ---------------------------------------------------------------------------


def test_update_metadata_writes_json(tmp_path: Path) -> None:
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    engine = _make_engine(sync_folder)
    engine.update_metadata()

    meta_path = sync_folder / "metadata.json"
    assert meta_path.exists()
    data = json.loads(meta_path.read_text(encoding="utf-8"))
    assert "last_sync" in data
    assert data["version"] == 1


def test_update_metadata_timestamp_is_iso_utc(tmp_path: Path) -> None:
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    engine = _make_engine(sync_folder)
    engine.update_metadata()

    data = json.loads((sync_folder / "metadata.json").read_text())
    # ISO format with timezone info (+00:00 suffix)
    assert "T" in data["last_sync"]
    assert "+00:00" in data["last_sync"] or "Z" in data["last_sync"]


# ---------------------------------------------------------------------------
# _install_external_extensions
# ---------------------------------------------------------------------------


def test_install_external_extensions_writes_stubs(tmp_path: Path) -> None:
    sync_profile = tmp_path / "sync_profile"
    sync_profile.mkdir(parents=True)
    ext_dir = tmp_path / "External Extensions"

    # Create manifest with Web Store extension IDs
    (sync_profile / "webstore_extensions.json").write_text(
        json.dumps(["aaabbbccc", "dddeeefff"]), encoding="utf-8"
    )

    engine = _make_engine(tmp_path)
    engine._install_external_extensions(sync_profile, ext_dir)

    assert (ext_dir / "aaabbbccc.json").exists()
    assert (ext_dir / "dddeeefff.json").exists()

    data = json.loads((ext_dir / "aaabbbccc.json").read_text())
    assert data == {"external_update_url": "https://clients2.google.com/service/update2/crx"}


def test_install_external_extensions_idempotent(tmp_path: Path) -> None:
    sync_profile = tmp_path / "sync_profile"
    sync_profile.mkdir(parents=True)
    ext_dir = tmp_path / "External Extensions"

    # Create manifest with one Web Store extension ID
    (sync_profile / "webstore_extensions.json").write_text(
        json.dumps(["aaabbbccc"]), encoding="utf-8"
    )

    engine = _make_engine(tmp_path)
    engine._install_external_extensions(sync_profile, ext_dir)
    # Write custom content to simulate an existing stub
    (ext_dir / "aaabbbccc.json").write_text('{"custom": true}', encoding="utf-8")
    engine._install_external_extensions(sync_profile, ext_dir)

    # Must not overwrite existing stub
    assert json.loads((ext_dir / "aaabbbccc.json").read_text()) == {"custom": True}


def test_install_external_extensions_no_extensions_dir(tmp_path: Path) -> None:
    sync_profile = tmp_path / "sync_profile"
    sync_profile.mkdir()
    ext_dir = tmp_path / "External Extensions"

    engine = _make_engine(tmp_path)
    # Should not raise when Extensions/ dir doesn't exist
    engine._install_external_extensions(sync_profile, ext_dir)
    assert not ext_dir.exists()


def test_sync_all_registers_external_extensions(tmp_path: Path) -> None:
    profile = tmp_path / "profiles" / "Default"
    ext_dir = tmp_path / "Ext"
    # Create a Web Store extension (will be registered but not synced)
    _make_ext_version_dir(profile, "testext", "1.0.0", "1.0.0", webstore=True)
    _write_file(profile / "Preferences", "{}", mtime=1000.0)

    browser = _make_browser(name="TB", installed=True, running=False, profiles=[profile])
    browser.external_extensions_dir.return_value = ext_dir

    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    engine = _make_engine(sync_folder, browsers=[browser])

    with patch("src.config.get_enabled_browsers", return_value={"TB": True}), \
         patch("src.config.get_enabled_profiles", return_value={"TB": ["Default"]}), \
         patch("src.config.get_profile_directions", return_value={}):
        engine.sync_all()

    assert (ext_dir / "testext.json").exists()


# ---------------------------------------------------------------------------
# Never-sync files excluded
# ---------------------------------------------------------------------------


def test_never_sync_files_excluded(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync_profile = tmp_path / "sync_profile"
    profile.mkdir()

    for name in NEVER_SYNC:
        _write_file(profile / name, "sensitive", mtime=9999.0)
    _write_file(profile / "Bookmarks", "safe", mtime=9999.0)

    engine = _make_engine(tmp_path)
    engine.sync_browser_profile(profile, sync_profile)

    # None of the sensitive files should appear in sync
    for name in NEVER_SYNC:
        assert not (sync_profile / name).exists(), f"{name} should not be synced"

    # Bookmarks must be synced
    assert (sync_profile / "Bookmarks").exists()


# ---------------------------------------------------------------------------
# Full round-trip integration test
# ---------------------------------------------------------------------------


def test_full_round_trip(tmp_path: Path) -> None:
    """Write profile files → sync → modify sync copy → sync back → verify profile updated."""
    profile = tmp_path / "profile"
    profile.mkdir()
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()

    # Step 1: populate profile
    bk_profile = profile / "Bookmarks"
    _write_file(bk_profile, "v1", mtime=1000.0)

    browser = _make_browser(name="Chrome", installed=True, running=False, profiles=[profile])
    engine = _make_engine(sync_folder, browsers=[browser])

    with patch("src.config.get_enabled_browsers", return_value={"Chrome": True}), \
         patch("src.config.get_enabled_profiles", return_value={"Chrome": [profile.name]}), \
         patch("src.config.get_profile_directions", return_value={}):
        engine.sync_all()

    # Verify sync folder received Bookmarks
    bk_sync = sync_folder / "current" / "Chrome" / profile.name / "Bookmarks"
    assert bk_sync.exists()
    assert bk_sync.read_text() == "v1"

    # Step 2: simulate user edits in sync folder (newer mtime)
    _write_file(bk_sync, "v2", mtime=3000.0)

    with patch("src.config.get_enabled_browsers", return_value={"Chrome": True}), \
         patch("src.config.get_enabled_profiles", return_value={"Chrome": [profile.name]}), \
         patch("src.config.get_profile_directions", return_value={}):
        engine.sync_all()

    # Verify profile was updated
    assert bk_profile.read_text() == "v2"


def test_sync_all_with_empty_config_skips_browser(tmp_path: Path) -> None:
    """When config has no entry for a browser, sync_all skips it entirely."""
    default = tmp_path / "profiles" / "Default"
    profile1 = tmp_path / "profiles" / "Profile 1"
    profile2 = tmp_path / "profiles" / "Profile 2"

    for p in [default, profile1, profile2]:
        p.mkdir(parents=True)
        _write_file(p / "Preferences", "{}", mtime=1000.0)

    browser = _make_browser(
        name="Chrome", installed=True, running=False, profiles=[default, profile1, profile2]
    )
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()
    engine = _make_engine(sync_folder, browsers=[browser])

    # Config is empty (no enabled_profiles entry for Chrome)
    with patch("src.config.get_enabled_browsers", return_value={}), \
         patch("src.config.get_enabled_profiles", return_value={}), \
         patch("src.config.get_profile_directions", return_value={}):
        engine.sync_all()

    # FIXED: No profiles should be synced when browser has no config entry
    current = sync_folder / "current"
    if current.exists():
        chrome_dir = current / "Chrome"
        assert not chrome_dir.exists(), "Chrome directory should not exist when no profiles enabled"


# ---------------------------------------------------------------------------
# find_rclone
# ---------------------------------------------------------------------------


def test_find_rclone_from_path() -> None:
    find_rclone.cache_clear()
    try:
        with patch("shutil.which", return_value="/usr/bin/rclone"):
            result = find_rclone()
        assert result == Path("/usr/bin/rclone")
    finally:
        find_rclone.cache_clear()


def test_find_rclone_fallback() -> None:
    find_rclone.cache_clear()
    try:
        with patch("shutil.which", return_value=None), \
             patch.object(Path, "exists", lambda self: str(self) == "/opt/homebrew/bin/rclone"):
            result = find_rclone()
        assert result == Path("/opt/homebrew/bin/rclone")
    finally:
        find_rclone.cache_clear()


def test_find_rclone_not_found() -> None:
    find_rclone.cache_clear()
    try:
        with patch("shutil.which", return_value=None), \
             patch.object(Path, "exists", return_value=False):
            result = find_rclone()
        assert result is None
    finally:
        find_rclone.cache_clear()
