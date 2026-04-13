from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.sync_engine import NEVER_SYNC, SyncEngine, _parse_version, find_rclone

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_archive(engine: SyncEngine, sync_folder: Path, files: dict[str, str]) -> Path:
    """Pack files dict {rel_path: content} into sync_folder/current.tar."""
    tmp = sync_folder / "_tmp_setup"
    tmp.mkdir(exist_ok=True)
    for rel, content in files.items():
        p = tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    archive = sync_folder / "current.tar"
    engine._pack_to_archive(tmp, archive)
    shutil.rmtree(tmp)
    return archive


def _file_in_archive(archive: Path, rel_path: str) -> bool:
    """Return True if rel_path exists inside a tar archive."""
    if not archive.is_file():
        return False
    with tarfile.open(str(archive)) as tf:
        names = tf.getnames()
    normalized = "./" + rel_path if not rel_path.startswith("./") else rel_path
    return normalized in names


def _read_from_archive(archive: Path, rel_path: str) -> str:
    """Read and return the decoded content of rel_path from a tar archive."""
    with tarfile.open(str(archive)) as tf:
        normalized = "./" + rel_path if not rel_path.startswith("./") else rel_path
        member = tf.extractfile(normalized)
        assert member is not None, f"{rel_path} not in archive"
        return member.read().decode("utf-8")


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
    mock.windows_extensions_registry_key.return_value = None
    mock.windows_force_list_registry_key.return_value = None
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

    # Pre-populate archive with older data
    engine = _make_engine(sync_folder)
    tmp_dir = sync_folder / "_setup"
    tmp_dir.mkdir()
    _write_file(tmp_dir / "Bookmarks", "sync-data", mtime=1000.0)
    _write_file(tmp_dir / "Preferences", "{}", mtime=1000.0)
    engine._pack_to_archive(tmp_dir, sync_folder / "current.tar")
    shutil.rmtree(tmp_dir)

    browser = _make_browser(
        name="TestBrowser", installed=True, running=False, profiles=[profile]
    )
    engine = _make_engine(sync_folder, browsers=[browser])

    directions = {"TestBrowser": {"Default": "push"}}
    with patch("src.config.get_enabled_browsers", return_value={"TestBrowser": True}), \
         patch("src.config.get_enabled_profiles", return_value={"TestBrowser": ["Default"]}), \
         patch("src.config.get_profile_directions", return_value=directions):
        engine.sync_all()

    # Push: profile (newer) → archive updated
    assert _read_from_archive(sync_folder / "current.tar", "Bookmarks") == "profile-data"
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
    _write_file(
        profile / "Preferences",
        '{"enable_do_not_track":true,"settings":{}}',
        mtime=2000.0,
    )
    _write_file(profile / "Custom Dictionary.txt", "word1\nword2", mtime=2000.0)

    engine = _make_engine(tmp_path)
    engine.sync_browser_profile(profile, sync_profile)

    assert (sync_profile / "Bookmarks").read_text() == '{"roots":{}}'
    saved = json.loads((sync_profile / "preferences.json").read_bytes())
    assert saved.get("enable_do_not_track") is True
    assert "settings" not in saved  # not in PREFERENCES_KEYS — must not be copied
    assert not (sync_profile / "Preferences").exists()  # raw file replaced by preferences.json
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
    assert (sync_profile / "preferences.json").exists()  # always synced


def test_sync_browser_profile_preferences_always_synced(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    sync_profile = tmp_path / "sync_profile"
    profile.mkdir()
    _write_file(profile / "Preferences", "{}", mtime=2000.0)

    engine = _make_engine(tmp_path)
    # Even with everything disabled, preferences.json must be written
    engine.sync_browser_profile(
        profile, sync_profile,
        {"extensions": False, "bookmarks": False, "custom_dictionary": False,
         "local_storage": False},
    )

    assert (sync_profile / "preferences.json").exists()


# ---------------------------------------------------------------------------
# sync_all
# ---------------------------------------------------------------------------


def test_sync_all_skips_running_browser(tmp_path: Path) -> None:
    browser = _make_browser(installed=True, running=True)
    engine = _make_engine(tmp_path, browsers=[browser])

    result = engine.sync_all()

    browser.discover_profiles.assert_not_called()
    assert browser.name in result["skipped_running"]


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

    assert (sync_folder / "current.tar").is_file()
    assert _read_from_archive(sync_folder / "current.tar", "Bookmarks") == '{"roots":{}}'


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

    assert _file_in_archive(sync_folder / "current.tar", "preferences.json")


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

    (sync_profile / "webstore_extensions.json").write_text(
        json.dumps(["aaabbbccc", "dddeeefff"]), encoding="utf-8"
    )

    browser = _make_browser()
    browser.external_extensions_dir.return_value = ext_dir

    engine = _make_engine(tmp_path)
    engine._install_external_extensions(sync_profile, browser)

    assert (ext_dir / "aaabbbccc.json").exists()
    assert (ext_dir / "dddeeefff.json").exists()

    data = json.loads((ext_dir / "aaabbbccc.json").read_text())
    assert data == {"external_update_url": "https://clients2.google.com/service/update2/crx"}


def test_install_external_extensions_idempotent(tmp_path: Path) -> None:
    sync_profile = tmp_path / "sync_profile"
    sync_profile.mkdir(parents=True)
    ext_dir = tmp_path / "External Extensions"

    (sync_profile / "webstore_extensions.json").write_text(
        json.dumps(["aaabbbccc"]), encoding="utf-8"
    )

    browser = _make_browser()
    browser.external_extensions_dir.return_value = ext_dir

    engine = _make_engine(tmp_path)
    engine._install_external_extensions(sync_profile, browser)
    # Write custom content to simulate an existing stub
    (ext_dir / "aaabbbccc.json").write_text('{"custom": true}', encoding="utf-8")
    engine._install_external_extensions(sync_profile, browser)

    # Must not overwrite existing stub
    assert json.loads((ext_dir / "aaabbbccc.json").read_text()) == {"custom": True}


def test_install_external_extensions_no_extensions_dir(tmp_path: Path) -> None:
    sync_profile = tmp_path / "sync_profile"
    sync_profile.mkdir()
    ext_dir = tmp_path / "External Extensions"

    browser = _make_browser()
    browser.external_extensions_dir.return_value = ext_dir

    engine = _make_engine(tmp_path)
    # Should not raise when no manifest exists; ext_dir must not be created
    engine._install_external_extensions(sync_profile, browser)
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
    """Write profile files → sync → modify archive → sync back → verify profile updated."""
    profile = tmp_path / "profile"
    profile.mkdir()
    sync_folder = tmp_path / "sync"
    sync_folder.mkdir()

    bk_profile = profile / "Bookmarks"
    _write_file(bk_profile, "v1", mtime=1000.0)

    browser = _make_browser(name="Chrome", installed=True, running=False, profiles=[profile])
    engine = _make_engine(sync_folder, browsers=[browser])

    with patch("src.config.get_enabled_browsers", return_value={"Chrome": True}), \
         patch("src.config.get_enabled_profiles", return_value={"Chrome": [profile.name]}), \
         patch("src.config.get_profile_directions", return_value={}):
        engine.sync_all()

    # Verify archive contains Bookmarks
    archive = sync_folder / "current.tar"
    assert archive.is_file()
    assert _read_from_archive(archive, "Bookmarks") == "v1"

    # Step 2: simulate user editing the archive content (unpack, modify, repack)
    edit_dir = tmp_path / "_edit"
    engine._unpack_archive(archive, edit_dir)
    _write_file(edit_dir / "Bookmarks", "v2", mtime=3000.0)
    engine._pack_to_archive(edit_dir, archive)
    shutil.rmtree(edit_dir)

    with patch("src.config.get_enabled_browsers", return_value={"Chrome": True}), \
         patch("src.config.get_enabled_profiles", return_value={"Chrome": [profile.name]}), \
         patch("src.config.get_profile_directions", return_value={}):
        engine.sync_all()

    # Verify profile was updated with v2
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

    # No profiles should be synced when browser has no config entry
    assert not (sync_folder / "current.tar").exists()
    current = sync_folder / "current"
    assert not current.exists() or not any(current.iterdir())


# ---------------------------------------------------------------------------
# _extract_search_shortcuts / _restore_search_shortcuts
# ---------------------------------------------------------------------------


def _make_web_data(path: Path, rows: list[dict]) -> None:
    """Create a minimal Web Data SQLite file with a keywords table."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE keywords (
            id INTEGER PRIMARY KEY,
            short_name VARCHAR NOT NULL,
            keyword VARCHAR NOT NULL,
            favicon_url VARCHAR NOT NULL DEFAULT '',
            url VARCHAR NOT NULL,
            safe_for_autoreplace INTEGER DEFAULT 0,
            originating_url VARCHAR DEFAULT '',
            date_created INTEGER DEFAULT 0,
            usage_count INTEGER DEFAULT 0,
            input_encodings VARCHAR DEFAULT 'UTF-8',
            suggest_url VARCHAR DEFAULT '',
            prepopulate_id INTEGER DEFAULT 0,
            created_by_policy INTEGER DEFAULT 0,
            last_modified INTEGER DEFAULT 0,
            sync_guid VARCHAR DEFAULT '',
            alternate_urls VARCHAR DEFAULT '[]',
            image_url VARCHAR DEFAULT '',
            search_url_post_params VARCHAR DEFAULT '',
            suggest_url_post_params VARCHAR DEFAULT '',
            image_url_post_params VARCHAR DEFAULT '',
            new_tab_url VARCHAR DEFAULT '',
            last_visited INTEGER DEFAULT 0,
            created_from_play_api INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1,
            starter_pack_id INTEGER DEFAULT 0,
            enforced_by_policy INTEGER DEFAULT 0,
            featured_by_policy INTEGER DEFAULT 0,
            url_hash BLOB
        )
        """
    )
    for row in rows:
        conn.execute(
            """
            INSERT INTO keywords
                (short_name, keyword, url, prepopulate_id, is_active,
                 sync_guid, safe_for_autoreplace, input_encodings, alternate_urls)
            VALUES (?,?,?,?,?,?,?,?,?)
            """,
            (
                row["short_name"], row["keyword"], row["url"],
                row.get("prepopulate_id", 0), row.get("is_active", 1),
                row.get("sync_guid", ""), row.get("safe_for_autoreplace", 0),
                row.get("input_encodings", "UTF-8"), row.get("alternate_urls", "[]"),
            ),
        )
    conn.commit()
    conn.close()


def test_extract_search_shortcuts_stores_sync_guid(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    _make_web_data(
        profile / "Web Data",
        [{"keyword": "yt", "short_name": "YouTube", "url": "https://yt.com/?q={searchTerms}",
          "sync_guid": "stable-guid-yt"}],
    )
    engine = _make_engine(tmp_path)
    engine._extract_search_shortcuts(profile, tmp_path)

    data = json.loads((tmp_path / "search_shortcuts.json").read_text())
    assert len(data) == 1
    assert data[0]["sync_guid"] == "stable-guid-yt"


def test_extract_search_shortcuts_excludes_prepopulated(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    _make_web_data(
        profile / "Web Data",
        [
            {"keyword": "custom", "short_name": "Custom", "url": "https://c.com/?q={searchTerms}",
             "prepopulate_id": 0},
            {"keyword": "google", "short_name": "Google", "url": "https://g.com/?q={searchTerms}",
             "prepopulate_id": 1},
        ],
    )
    engine = _make_engine(tmp_path)
    engine._extract_search_shortcuts(profile, tmp_path)

    data = json.loads((tmp_path / "search_shortcuts.json").read_text())
    assert len(data) == 1
    assert data[0]["keyword"] == "custom"


def test_restore_search_shortcuts_uses_sync_guid(tmp_path: Path) -> None:
    shortcuts_json = tmp_path / "search_shortcuts.json"
    shortcuts_json.write_text(
        json.dumps([{
            "keyword": "yt", "short_name": "YouTube",
            "url": "https://yt.com/?q={searchTerms}",
            "sync_guid": "my-stable-guid",
        }]),
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    _make_web_data(profile / "Web Data", [])

    engine = _make_engine(tmp_path)
    engine._restore_search_shortcuts(profile, tmp_path)

    conn = sqlite3.connect(str(profile / "Web Data"))
    row = conn.execute("SELECT sync_guid FROM keywords WHERE keyword='yt'").fetchone()
    conn.close()
    assert row is not None
    assert row[0] == "my-stable-guid"


def test_restore_search_shortcuts_empty_guid_when_missing(tmp_path: Path) -> None:
    shortcuts_json = tmp_path / "search_shortcuts.json"
    shortcuts_json.write_text(
        json.dumps([{
            "keyword": "yt", "short_name": "YouTube",
            "url": "https://yt.com/?q={searchTerms}",
        }]),
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    _make_web_data(profile / "Web Data", [])

    engine = _make_engine(tmp_path)
    engine._restore_search_shortcuts(profile, tmp_path)

    conn = sqlite3.connect(str(profile / "Web Data"))
    row = conn.execute("SELECT sync_guid FROM keywords WHERE keyword='yt'").fetchone()
    conn.close()
    assert row is not None
    # Empty sync_guid keeps the engine local-only (not subject to sync deletion).
    assert row[0] == ""


def test_extract_search_shortcuts_marks_default(tmp_path: Path) -> None:
    profile = tmp_path / "profile"
    _make_web_data(
        profile / "Web Data",
        [
            {"keyword": "gru", "short_name": "Google RU", "url": "https://g.ru/?q={searchTerms}",
             "sync_guid": "default-guid-123"},
            {"keyword": "yt", "short_name": "YouTube", "url": "https://yt.com/?q={searchTerms}",
             "sync_guid": "other-guid-456"},
        ],
    )
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Preferences").write_text(
        json.dumps({"default_search_provider": {"guid": "default-guid-123"}}),
        encoding="utf-8",
    )
    engine = _make_engine(tmp_path)
    engine._extract_search_shortcuts(profile, tmp_path)

    data = json.loads((tmp_path / "search_shortcuts.json").read_text())
    by_keyword = {s["keyword"]: s for s in data}
    assert by_keyword["gru"]["is_default"] is True
    assert by_keyword["yt"]["is_default"] is False


def test_extract_search_shortcuts_adopts_prefs_guid_when_db_guid_empty(tmp_path: Path) -> None:
    """Default engine with empty sync_guid in DB should adopt the guid from Preferences."""
    profile = tmp_path / "profile"
    _make_web_data(
        profile / "Web Data",
        [
            {"keyword": "gru", "short_name": "Google RU",
             "url": "https://g.ru/?q={searchTerms}", "sync_guid": ""},
            {"keyword": "yt", "short_name": "YouTube",
             "url": "https://yt.com/?q={searchTerms}", "sync_guid": ""},
        ],
    )
    profile.mkdir(parents=True, exist_ok=True)
    (profile / "Preferences").write_text(
        json.dumps({
            "default_search_provider": {"guid": "known-guid-from-prefs"},
            "default_search_provider_data": {
                "mirrored_template_url_data": {"url": "https://g.ru/?q={searchTerms}"},
            },
        }),
        encoding="utf-8",
    )

    engine = _make_engine(tmp_path)
    engine._extract_search_shortcuts(profile, tmp_path)

    data = json.loads((tmp_path / "search_shortcuts.json").read_text())
    by_keyword = {s["keyword"]: s for s in data}
    # Guid from Preferences is adopted when the DB row has no guid.
    assert by_keyword["gru"]["sync_guid"] == "known-guid-from-prefs"
    assert by_keyword["gru"]["is_default"] is True
    # Non-default engine stays without a guid.
    assert by_keyword["yt"]["sync_guid"] == ""
    assert by_keyword["yt"]["is_default"] is False


def test_extract_search_shortcuts_no_webdata_preserves_existing_json(tmp_path: Path) -> None:
    """Missing Web Data does not wipe the existing search_shortcuts.json."""
    existing = [{"keyword": "gru", "short_name": "Google Russia", "url": "https://gru.com",
                 "is_default": True, "sync_guid": "gru-guid", "prepopulate_id": 0,
                 "is_active": 1, "date_created": 0, "last_modified": 0,
                 "safe_for_autoreplace": 0, "input_encodings": "UTF-8",
                 "alternate_urls": "[]", "favicon_url": "", "suggest_url": ""}]
    (tmp_path / "search_shortcuts.json").write_text(json.dumps(existing), encoding="utf-8")

    profile = tmp_path / "profile_no_webdata"  # no Web Data file here
    engine = _make_engine(tmp_path)
    engine._extract_search_shortcuts(profile, tmp_path)

    data = json.loads((tmp_path / "search_shortcuts.json").read_text())
    assert len(data) == 1
    assert data[0]["keyword"] == "gru", "existing JSON must be untouched when Web Data is missing"


def test_sync_browser_profile_both_direction_does_not_extract_shortcuts(tmp_path: Path) -> None:
    """In 'both' direction the JSON is treated as the master and is never overwritten."""
    sf = tmp_path / "sync"
    sf.mkdir()
    existing = [{"keyword": "gru", "short_name": "GRU", "url": "https://gru.com",
                 "is_default": True, "sync_guid": "gru-guid", "prepopulate_id": 0,
                 "is_active": 1, "date_created": 0, "last_modified": 0,
                 "safe_for_autoreplace": 0, "input_encodings": "UTF-8",
                 "alternate_urls": "[]", "favicon_url": "", "suggest_url": ""}]
    (sf / "search_shortcuts.json").write_text(json.dumps(existing), encoding="utf-8")

    profile = tmp_path / "profile"
    _make_web_data(
        profile / "Web Data",
        [{"keyword": "bing", "short_name": "Bing", "url": "https://bing.com/?q={searchTerms}"}],
    )
    (profile / "Preferences").write_text("{}", encoding="utf-8")

    engine = _make_engine(sf)
    engine.sync_browser_profile(profile, tmp_path / "sync_profile", direction="both")

    data = json.loads((sf / "search_shortcuts.json").read_text())
    keywords = {s["keyword"] for s in data}
    assert "gru" in keywords, "master JSON shortcut must survive a 'both' sync"
    assert "bing" not in keywords, "local-only shortcut must not be pushed in 'both' direction"


def test_sync_browser_profile_push_direction_does_extract_shortcuts(tmp_path: Path) -> None:
    """In 'push' direction the local browser's shortcuts overwrite the JSON."""
    sf = tmp_path / "sync"
    sf.mkdir()
    (sf / "search_shortcuts.json").write_text(
        json.dumps([{"keyword": "old", "short_name": "Old", "url": "https://old.com",
                     "is_default": False, "sync_guid": "", "prepopulate_id": 0,
                     "is_active": 1, "date_created": 0, "last_modified": 0,
                     "safe_for_autoreplace": 0, "input_encodings": "UTF-8",
                     "alternate_urls": "[]", "favicon_url": "", "suggest_url": ""}]),
        encoding="utf-8",
    )

    profile = tmp_path / "profile"
    _make_web_data(
        profile / "Web Data",
        [{"keyword": "new", "short_name": "New", "url": "https://new.com/?q={searchTerms}"}],
    )
    (profile / "Preferences").write_text("{}", encoding="utf-8")

    engine = _make_engine(sf)
    engine.sync_browser_profile(profile, tmp_path / "sync_profile", direction="push")

    data = json.loads((sf / "search_shortcuts.json").read_text())
    keywords = {s["keyword"] for s in data}
    assert "new" in keywords
    assert "old" not in keywords, "push must overwrite JSON with local shortcuts"


def test_restore_search_shortcuts_preserves_stored_guid_and_updates_preferences(
    tmp_path: Path,
) -> None:
    shortcuts_json = tmp_path / "search_shortcuts.json"
    shortcuts_json.write_text(
        json.dumps([
            {"keyword": "gru", "short_name": "Google RU",
             "url": "https://g.ru/?q={searchTerms}",
             "sync_guid": "old-guid", "is_default": True},
            {"keyword": "yt", "short_name": "YouTube",
             "url": "https://yt.com/?q={searchTerms}",
             "sync_guid": "yt-guid", "is_default": False},
        ]),
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    _make_web_data(profile / "Web Data", [])
    prefs_path = profile / "Preferences"
    prefs_path.write_text(
        json.dumps({"default_search_provider": {"guid": "old-prefs-guid"}}),
        encoding="utf-8",
    )

    engine = _make_engine(tmp_path)
    engine._restore_search_shortcuts(profile, tmp_path)

    conn = sqlite3.connect(str(profile / "Web Data"))
    rows = {r[0]: r[1] for r in conn.execute("SELECT keyword, sync_guid FROM keywords")}
    conn.close()
    # Stored guid from JSON is preserved, not overridden with current Preferences guid.
    assert rows["gru"] == "old-guid"
    assert rows["yt"] == "yt-guid"
    # Preferences is updated to point at the default engine's stored guid.
    prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
    assert prefs["default_search_provider"]["guid"] == "old-guid"


def test_make_url_hash_returns_valid_64_byte_blob() -> None:
    """_make_url_hash must return exactly 64 bytes starting with b'v10', decryptable."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = os.urandom(32)
    aesgcm = AESGCM(key)
    blob = SyncEngine._make_url_hash(42, "https://example.com/?q={searchTerms}", aesgcm)
    assert len(blob) == 64
    assert blob[:3] == b"v10"
    # Decrypt and verify plaintext: b'\x01' + 32-byte SHA-256 hash
    nonce = blob[3:15]
    plaintext = aesgcm.decrypt(nonce, blob[15:], None)
    assert len(plaintext) == 33
    assert plaintext[0:1] == b"\x01"


def test_restore_search_shortcuts_writes_url_hash_when_key_available(tmp_path: Path) -> None:
    """url_hash must be a valid 64-byte v10 blob when the OSCrypt key is available."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aesgcm = AESGCM(os.urandom(32))
    shortcuts_json = tmp_path / "search_shortcuts.json"
    shortcuts_json.write_text(
        json.dumps([{"keyword": "yt", "short_name": "YouTube",
                     "url": "https://yt.com/?q={searchTerms}"}]),
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    _make_web_data(profile / "Web Data", [])

    engine = _make_engine(tmp_path)
    with patch.object(SyncEngine, "_load_oscrypt_key", return_value=aesgcm):
        engine._restore_search_shortcuts(profile, tmp_path)

    conn = sqlite3.connect(str(profile / "Web Data"))
    blob = conn.execute("SELECT url_hash FROM keywords WHERE keyword='yt'").fetchone()[0]
    conn.close()
    assert isinstance(blob, bytes)
    assert len(blob) == 64
    assert blob[:3] == b"v10"


def test_restore_search_shortcuts_url_hash_encodes_correct_id_and_url(tmp_path: Path) -> None:
    """url_hash plaintext must encode the actual DB row id and the engine url."""
    import hashlib
    import struct

    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    aesgcm = AESGCM(os.urandom(32))
    url = "https://yt.com/?q={searchTerms}"
    shortcuts_json = tmp_path / "search_shortcuts.json"
    shortcuts_json.write_text(
        json.dumps([{"keyword": "yt", "short_name": "YouTube", "url": url}]),
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    _make_web_data(profile / "Web Data", [])

    engine = _make_engine(tmp_path)
    with patch.object(SyncEngine, "_load_oscrypt_key", return_value=aesgcm):
        engine._restore_search_shortcuts(profile, tmp_path)

    conn = sqlite3.connect(str(profile / "Web Data"))
    row_id, blob = conn.execute("SELECT id, url_hash FROM keywords WHERE keyword='yt'").fetchone()
    conn.close()

    # Decrypt and verify the plaintext encodes the actual row id and url
    nonce = blob[3:15]
    plaintext = aesgcm.decrypt(nonce, blob[15:], None)
    url_b = url.encode("utf-8")
    pad = (4 - len(url_b) % 4) % 4
    payload = struct.pack("<q", row_id) + struct.pack("<I", len(url_b)) + url_b + bytes(pad)
    pickle_bytes = struct.pack("<I", len(payload)) + payload
    expected_plaintext = b"\x01" + hashlib.sha256(pickle_bytes).digest()
    assert plaintext == expected_plaintext


def test_restore_search_shortcuts_null_url_hash_without_key(tmp_path: Path) -> None:
    """url_hash stays NULL when no OSCrypt key is available (non-Windows or error)."""
    shortcuts_json = tmp_path / "search_shortcuts.json"
    shortcuts_json.write_text(
        json.dumps([{"keyword": "yt", "short_name": "YouTube",
                     "url": "https://yt.com/?q={searchTerms}"}]),
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    _make_web_data(profile / "Web Data", [])

    engine = _make_engine(tmp_path)
    with patch.object(SyncEngine, "_load_oscrypt_key", return_value=None):
        engine._restore_search_shortcuts(profile, tmp_path)

    conn = sqlite3.connect(str(profile / "Web Data"))
    blob = conn.execute("SELECT url_hash FROM keywords WHERE keyword='yt'").fetchone()[0]
    conn.close()
    assert blob is None


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
    fake_path = Path("/fake/rclone")
    try:
        with patch("shutil.which", return_value=None), \
             patch("src.sync_engine._FALLBACK_PATHS", [fake_path]), \
             patch.object(Path, "exists", lambda self: self == fake_path):
            result = find_rclone()
        assert result == fake_path
    finally:
        find_rclone.cache_clear()


@pytest.mark.skipif(sys.platform != "win32", reason="Tests Windows-specific fallback paths")
def test_find_rclone_fallback_windows() -> None:
    find_rclone.cache_clear()
    expected = "C:/Program Files/rclone/rclone.exe"
    try:
        with patch("shutil.which", return_value=None), \
             patch.object(Path, "exists", lambda self: str(self).replace("\\", "/") == expected):
            result = find_rclone()
        assert result == Path(expected)
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
