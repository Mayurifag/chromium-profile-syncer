from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.operations import operation_lock
from src.settings import _desktop_backup
from src.sync import extensions, sync_dir
from src.sync_engine import SyncEngine

A, B = "a" * 32, "b" * 32


def write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def browser_for(profile):
    browser = MagicMock()
    browser.name = "Test"
    browser.is_installed.return_value = True
    browser.is_running.return_value = False
    browser.discover_profiles.return_value = [profile]
    browser.local_state_path.return_value = None
    browser.ext_id_aliases = {}
    return browser


def test_corrupt_desktop_archive_preserves_good_backup(tmp_path, monkeypatch):
    current, archive = tmp_path / "current", tmp_path / "backup.tar.gz"
    write(current / "Bookmarks", "good backup")
    archive.write_bytes(b"not a gzip archive")
    monkeypatch.setattr(_desktop_backup, "desktop_backup_path", lambda: archive)
    with pytest.raises(tarfile.ReadError):
        _desktop_backup.restore(current)
    assert (current / "Bookmarks").read_text() == "good backup"


@pytest.mark.parametrize("name", ["../escape", "/absolute", "current/../../escape"])
def test_unsafe_archive_does_not_touch_backup(tmp_path, monkeypatch, name):
    current, archive = tmp_path / "current", tmp_path / "backup.tar.gz"
    write(current / "Bookmarks", "good backup")
    with tarfile.open(archive, "w:gz") as stream:
        member = tarfile.TarInfo(name)
        member.size = 3
        stream.addfile(member, io.BytesIO(b"bad"))
    monkeypatch.setattr(_desktop_backup, "desktop_backup_path", lambda: archive)
    with pytest.raises(ValueError):
        _desktop_backup.restore(current)
    assert (current / "Bookmarks").read_text() == "good backup"


def test_desktop_round_trip(tmp_path, monkeypatch):
    current, archive = tmp_path / "current", tmp_path / "backup.tar.gz"
    write(current / "Bookmarks", "saved")
    sync_dir.mark_changed(current)
    monkeypatch.setattr(_desktop_backup, "desktop_backup_path", lambda: archive)
    _desktop_backup.create(current)
    write(current / "Bookmarks", "changed")
    _desktop_backup.restore(current)
    assert (current / "Bookmarks").read_text() == "saved"
    sync_dir.validate_snapshot(current)


def test_failed_desktop_creation_preserves_previous_archive(tmp_path, monkeypatch):
    current, archive = tmp_path / "current", tmp_path / "backup.tar.gz"
    write(current / "Bookmarks", "saved")
    archive.write_bytes(b"previous archive")
    monkeypatch.setattr(_desktop_backup, "desktop_backup_path", lambda: archive)
    with (
        patch("src.settings._desktop_backup.seed_work_dir", side_effect=OSError("copy failed")),
        pytest.raises(OSError),
    ):
        _desktop_backup.create(current)
    assert archive.read_bytes() == b"previous archive"


def test_failed_publication_keeps_previous_snapshot(tmp_path):
    current, work = tmp_path / "current", tmp_path / "work"
    write(current / "Bookmarks", "good backup")
    write(work / "Bookmarks", "replacement")

    def partial_copy(source, destination):
        write(destination, "partial")
        raise OSError("disk full")

    with (
        patch("src.sync.sync_dir.shutil.copy2", side_effect=partial_copy),
        pytest.raises(OSError, match="disk full"),
    ):
        sync_dir.merge_to_sync_dir(work, current)
    assert (current / "Bookmarks").read_text() == "good backup"
    assert not list(tmp_path.glob(".current-publish-*"))


def test_partial_cloud_snapshot_is_not_restored(tmp_path):
    current = tmp_path / "current"
    write(current / "Bookmarks", "complete")
    sync_dir.mark_changed(current)
    write(current / "Bookmarks", "partial cloud download")
    with pytest.raises(sync_dir.SnapshotConflictError):
        sync_dir.seed_work_dir(current, tmp_path / "work")


def test_shortcuts_save_publishes_valid_snapshot(tmp_path, qapp):
    from src.shortcuts_editor import ShortcutsEditorDialog

    current = tmp_path / "current"
    path = current / "search_shortcuts.json"
    write(path, '[{"keyword":"test","short_name":"Test","url":"https://example.invalid"}]')
    write(current / "Bookmarks", "keep bookmarks")
    sync_dir.mark_changed(current)
    before = (current / "metadata.json").read_bytes()
    dialog = ShortcutsEditorDialog(shortcuts_json_path=path)
    dialog.table.item(0, 1).setText("Changed")
    with patch("src.shortcuts_editor.QMessageBox.information"):
        dialog._save_shortcuts()
    assert json.loads(path.read_text())[0]["short_name"] == "Changed"
    assert (current / "metadata.json").read_bytes() != before
    sync_dir.seed_work_dir(current, tmp_path / "work")
    assert (tmp_path / "work" / "Bookmarks").read_text() == "keep bookmarks"


def test_failed_shortcuts_publication_preserves_snapshot(tmp_path, qapp):
    from src.shortcuts_editor import ShortcutsEditorDialog

    current = tmp_path / "current"
    path = current / "search_shortcuts.json"
    write(path, '[{"keyword":"test","short_name":"Test","url":"https://example.invalid"}]')
    sync_dir.mark_changed(current)
    before = path.read_bytes(), (current / "metadata.json").read_bytes()
    dialog = ShortcutsEditorDialog(shortcuts_json_path=path)
    dialog.table.item(0, 1).setText("Changed")
    with (
        patch("src.sync.sync_dir.merge_to_sync_dir", side_effect=OSError("disk full")),
        patch("src.shortcuts_editor.QMessageBox.critical") as error,
        patch("src.shortcuts_editor.QMessageBox.information") as success,
    ):
        dialog._save_shortcuts()
    error.assert_called_once()
    success.assert_not_called()
    assert (path.read_bytes(), (current / "metadata.json").read_bytes()) == before
    sync_dir.validate_snapshot(current)


def test_flags_dialog_reports_publication_failure(tmp_path, qapp):
    from src.settings.flags_manager import FlagsManagerDialog

    current = tmp_path / "current"
    write(
        current / "browser_flags.json",
        '{"Test":{"enabled_labs_experiments":["drop@1"],"mtime":1}}',
    )
    sync_dir.mark_changed(current)
    before = (current / "metadata.json").read_bytes()
    with patch("src.settings.flags_manager.ALL_BROWSERS", []):
        dialog = FlagsManagerDialog(None, sync_folder=tmp_path)
        dialog._remove_checks["drop@1"].setChecked(True)
        with (
            patch("src.sync.sync_dir.merge_to_sync_dir", side_effect=OSError("disk full")),
            patch("src.settings.flags_manager.QMessageBox.critical") as error,
        ):
            dialog._save()
    error.assert_called_once()
    assert dialog.result() != dialog.DialogCode.Accepted
    assert (current / "metadata.json").read_bytes() == before
    assert "flags_ignore" not in config.load()
    sync_dir.validate_snapshot(current)


def test_restoring_one_extension_does_not_overwrite_another(tmp_path):
    profile, work = tmp_path / "profile", tmp_path / "work"
    write(work / "webstore_extensions.json", json.dumps({A: "One", B: "Two"}))
    write(
        profile / "Secure Preferences",
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        A: {"state": 1},
                        B: {"state": 1},
                    }
                }
            }
        ),
    )
    for eid in (A, B):
        write(work / "Local Extension Settings" / eid / "data.ldb", "old saved settings")
        write(profile / "Local Extension Settings" / eid / "data.ldb", "new local settings")
    engine = SyncEngine(tmp_path, browsers=[])
    completed = engine._sync_single_profile(
        browser_for(profile),
        profile,
        work,
        "both",
        False,
        frozenset({A}),
        {},
        True,
    )
    assert completed == {A}
    assert (profile / "Local Extension Settings" / A / "data.ldb").read_text() == (
        "old saved settings"
    )
    assert (profile / "Local Extension Settings" / B / "data.ldb").read_text() == (
        "new local settings"
    )
    assert B in extensions.extension_install_ids(work)


def test_newly_installed_extension_gets_saved_settings(tmp_path):
    profile, work = tmp_path / "profile", tmp_path / "work"
    write(work / "webstore_extensions.json", json.dumps({A: "One"}))
    write(
        profile / "Secure Preferences",
        json.dumps(
            {
                "extensions": {
                    "settings": {
                        A: {"state": 1},
                    }
                }
            }
        ),
    )
    config.set_extension_observation(profile, [])
    write(work / "Local Extension Settings" / A / "data.ldb", "saved settings")
    write(profile / "Local Extension Settings" / A / "data.ldb", "browser defaults")
    engine = SyncEngine(tmp_path, browsers=[])
    assert engine._sync_single_profile(
        browser_for(profile),
        profile,
        work,
        "both",
        False,
        frozenset(),
        {},
        True,
    ) == {A}
    assert (profile / "Local Extension Settings" / A / "data.ldb").read_text() == "saved settings"


def test_paused_profile_is_not_automatically_uploaded(tmp_path):
    paused, active = tmp_path / "Paused", tmp_path / "Active"
    paused.mkdir()
    active.mkdir()
    browser = browser_for(paused)
    browser.discover_profiles.return_value = [paused, active]
    config.set_enabled_profiles({"Test": ["Paused", "Active"]})
    config.set_profile_sync_enabled("Test", "Paused", False)
    engine = SyncEngine(tmp_path / "sync", browsers=[browser])
    with (
        patch.object(engine, "_sync_single_profile", return_value=set()) as sync,
        patch("src.sync_engine.install_external_extensions"),
        patch("src.sync_engine._extensions.record_observation"),
    ):
        engine.sync_all()
        assert [call.args[1] for call in sync.call_args_list] == [active]


def test_unmanaged_browser_flags_are_not_modified(tmp_path):
    profile = tmp_path / "Default"
    profile.mkdir()
    managed, unmanaged = browser_for(profile), browser_for(profile)
    unmanaged.name = "Unmanaged"
    state = tmp_path / "Unmanaged Local State"
    write(state, '{"browser":{"enabled_labs_experiments":["keep-me"]}}')
    unmanaged.local_state_path.return_value = state
    config.set_enabled_profiles({"Test": ["Default"]})
    engine = SyncEngine(tmp_path / "sync", browsers=[managed, unmanaged])
    with patch("src.sync_engine.install_external_extensions"):
        engine.sync_all()
    assert json.loads(state.read_text())["browser"]["enabled_labs_experiments"] == ["keep-me"]


def test_operations_are_exclusive_across_processes():
    script = (
        "import sys; from pathlib import Path; from src import config; "
        "from src.operations import operation_lock, OperationBusyError; "
        "config.CONFIG_PATH=Path(sys.argv[1]);\n"
        "try:\n with operation_lock(): pass\n"
        "except OperationBusyError: sys.exit(42)\n"
    )
    with operation_lock():
        result = subprocess.run([sys.executable, "-c", script, str(config.CONFIG_PATH)])
        assert result.returncode == 42
    with operation_lock():
        with operation_lock():
            config.set_autostart(False)
    assert not config.get_autostart()


def test_pending_restore_does_not_archive_an_extension_before_installation(tmp_path):
    profile, work = tmp_path / "Default", tmp_path / "work"
    write(work / "webstore_extensions.json", json.dumps({A: "One"}))
    write(profile / "Secure Preferences", '{"extensions":{"settings":{}}}')
    config.set_extension_observation(profile, [A])
    extensions.update_webstore_manifest(profile, work, ignore_removals={A})
    assert A in extensions.extension_install_ids(work)


def test_repeated_typed_url_restoration_is_idempotent(tmp_path):
    from src.sync.history import restore_typed_urls

    profile = tmp_path / "Default"
    profile.mkdir()
    with sqlite3.connect(profile / "History") as connection:
        connection.execute(
            "CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, "
            "visit_count INTEGER, typed_count INTEGER, "
            "last_visit_time INTEGER, hidden INTEGER)"
        )
        connection.execute("INSERT INTO urls VALUES (1, 'https://example.invalid', '', 1, 1, 1, 0)")
    write(
        tmp_path / "typed_urls.json",
        json.dumps(
            [
                {"url": "https://example.invalid", "typed_count": 1, "last_visit_time": 1},
            ]
        ),
    )
    for _ in range(3):
        restore_typed_urls(profile, tmp_path)
    with sqlite3.connect(profile / "History") as connection:
        assert connection.execute("SELECT typed_count FROM urls").fetchone() == (1,)
