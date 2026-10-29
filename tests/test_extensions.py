from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import config
from src.sync import extensions
from src.sync.extension_install import install_external_extensions
from src.sync.sync_dir import merge_to_sync_dir, seed_work_dir
from src.sync_engine import SyncEngine

EXT_ID = "a" * 32
OTHER_ID = "b" * 32


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    path = tmp_path.parent / f"{tmp_path.name}-config.json"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    yield
    path.unlink(missing_ok=True)


def _write(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def _profile(path: Path, ids: dict[str, int]) -> None:
    _write(
        path / "Secure Preferences",
        {"extensions": {"settings": {ext_id: {"state": state} for ext_id, state in ids.items()}}},
    )


def test_removal_archives_last_backup_and_does_not_return_from_stale_browser(tmp_path):
    sync = tmp_path / "sync"
    first, second = tmp_path / "first", tmp_path / "second"
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example"})
    data = sync / "Local Extension Settings" / EXT_ID / "data.ldb"
    data.parent.mkdir(parents=True)
    data.write_text("last saved settings")
    for profile in (first, second):
        _profile(profile, {EXT_ID: 1})
        extensions.record_observation(profile)
    _profile(first, {})
    extensions.update_webstore_manifest(first, sync)
    assert extensions.read_library(sync)[EXT_ID]["archived"]
    assert EXT_ID not in extensions.extension_install_ids(sync)
    extensions.update_webstore_manifest(second, sync)
    assert extensions.read_library(sync)[EXT_ID]["archived"]
    assert data.read_text() == "last saved settings"
    extensions.set_archived(sync, {EXT_ID}, False)
    assert EXT_ID in extensions.extension_install_ids(sync)


def test_disabled_and_blocklisted_extensions_are_not_treated_as_removed(tmp_path):
    sync, profile = tmp_path / "sync", tmp_path / "profile"
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example", OTHER_ID: "Other"})
    _profile(profile, {EXT_ID: 1, OTHER_ID: 1})
    extensions.record_observation(profile)
    _profile(profile, {EXT_ID: 0, OTHER_ID: 3})
    extensions.update_webstore_manifest(profile, sync)
    assert set(extensions.read_manifest(sync)) == {EXT_ID, OTHER_ID}


def test_external_uninstall_tombstone_is_a_removal(tmp_path):
    sync, profile = tmp_path / "sync", tmp_path / "profile"
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example"})
    _profile(profile, {EXT_ID: 1})
    extensions.record_observation(profile)
    _profile(profile, {EXT_ID: 2})
    extensions.update_webstore_manifest(profile, sync)
    assert extensions.read_library(sync)[EXT_ID]["archived"]


def test_new_empty_profile_does_not_archive_another_profiles_extensions(tmp_path):
    sync, profile = tmp_path / "sync", tmp_path / "profile"
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example"})
    _profile(profile, {})
    extensions.update_webstore_manifest(profile, sync)
    assert EXT_ID in extensions.read_manifest(sync)


def test_corrupt_browser_state_aborts_instead_of_archiving_everything(tmp_path):
    sync, profile = tmp_path / "sync", tmp_path / "profile"
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example"})
    _profile(profile, {EXT_ID: 1})
    extensions.record_observation(profile)
    (profile / "Secure Preferences").write_text("broken")
    with pytest.raises(ValueError):
        extensions.update_webstore_manifest(profile, sync)
    assert EXT_ID in extensions.read_manifest(sync)


def test_alias_removal_archives_canonical_record_and_filters_both_ids(tmp_path):
    sync, profile = tmp_path / "sync", tmp_path / "profile"
    aliases = {OTHER_ID: EXT_ID}
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example"})
    _profile(profile, {OTHER_ID: 1})
    extensions.record_observation(profile, aliases)
    _profile(profile, {})
    extensions.update_webstore_manifest(profile, sync, aliases)
    assert extensions.archived_ids(sync, aliases) == {EXT_ID, OTHER_ID}


def test_orphan_data_is_saved_not_deleted(tmp_path):
    data = tmp_path / "Local Extension Settings" / EXT_ID / "data.ldb"
    data.parent.mkdir(parents=True)
    data.write_text("recover me")
    assert extensions.preserve_orphan_extension_data(tmp_path, set()) == {EXT_ID}
    assert data.read_text() == "recover me"
    assert extensions.read_library(tmp_path)[EXT_ID]["archived"]


def test_archived_settings_are_neither_pulled_nor_overwritten(tmp_path):
    profile, sync = tmp_path / "profile", tmp_path / "sync"
    extensions.set_archived(sync, {EXT_ID}, True)
    relative = Path("Local Extension Settings") / EXT_ID / "data.ldb"
    (sync / relative).parent.mkdir(parents=True)
    (sync / relative).write_text("saved")
    _profile(profile, {})
    engine = SyncEngine(tmp_path, browsers=[])
    engine.sync_browser_profile(profile, sync)
    assert not (profile / relative).exists()
    (profile / relative).parent.mkdir(parents=True)
    (profile / relative).write_text("stale browser data")
    engine.sync_browser_profile(profile, sync)
    assert (sync / relative).read_text() == "saved"


def test_archive_unregisters_only_known_stubs(tmp_path):
    sync, stubs = tmp_path / "sync", tmp_path / "stubs"
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example"})
    browser = MagicMock()
    browser.name = "Test"
    browser.ext_id_aliases = {}
    browser.ungoogled = False
    browser.web_store_update_url = "https://clients2.google.com/service/update2/crx"
    browser.external_extensions_dir.return_value = stubs
    browser.linux_managed_policy_dir.return_value = None
    with patch("src.sync.extension_install.platform.system", return_value="Linux"):
        install_external_extensions(sync, browser, ungoogled_only_ext_ids=[])
        _write(stubs / f"{OTHER_ID}.json", {"external_update_url": "https://other.invalid"})
        extensions.set_archived(sync, {EXT_ID}, True)
        install_external_extensions(sync, browser, ungoogled_only_ext_ids=[])
    assert not (stubs / f"{EXT_ID}.json").exists()
    assert (stubs / f"{OTHER_ID}.json").exists()


def test_invalid_manifest_ids_cannot_write_outside_stubs_directory(tmp_path):
    sync = tmp_path / "sync"
    _write(sync / "webstore_extensions.json", {"../escape": "Invalid"})
    browser = MagicMock()
    browser.name = "Test"
    browser.ext_id_aliases = {}
    browser.external_extensions_dir.return_value = tmp_path / "stubs"
    browser.linux_managed_policy_dir.return_value = None
    with patch("src.sync.extension_install.platform.system", return_value="Linux"):
        install_external_extensions(sync, browser, ungoogled_only_ext_ids=[])
    assert not (tmp_path / "escape.json").exists()
    assert not (tmp_path / "stubs").exists()


def test_merge_does_not_overwrite_root_json_written_during_sync(tmp_path):
    current, work = tmp_path / "current", tmp_path / "work"
    _write(current / "browser_flags.json", {"before": True})
    baseline = seed_work_dir(current, work)
    _write(current / "browser_flags.json", {"after": True})
    with pytest.raises(OSError, match="changed during"):
        merge_to_sync_dir(work, current, baseline)
    assert json.loads((current / "browser_flags.json").read_text()) == {"after": True}


def test_restore_transfer_failure_leaves_existing_profile_data(tmp_path):
    profile, sync = tmp_path / "profile", tmp_path / "sync"
    for directory, value in ((profile, "original"), (sync, "replacement")):
        directory.mkdir()
        (directory / "Bookmarks").write_text(value)
    engine = SyncEngine(tmp_path, browsers=[])
    with (
        patch("src.sync_engine.shutil.copy2", side_effect=OSError("transfer failed")),
        pytest.raises(OSError, match="transfer failed"),
    ):
        engine.restore_profile_from_backup(profile, sync)
    assert (profile / "Bookmarks").read_text() == "original"
    assert not list(tmp_path.glob("cps-restore-*"))


def test_policy_release_preserves_unrelated_and_url_bearing_values(tmp_path, monkeypatch):
    from src.sync.extension_install import release_forced_extensions

    registry = MagicMock()
    registry.EnumValue.side_effect = [
        ("1", EXT_ID, 1),
        ("2", OTHER_ID, 1),
        ("3", f"{EXT_ID};https://other.invalid", 1),
        ("4", [EXT_ID], 7),
        OSError(),
    ]
    monkeypatch.setitem(sys.modules, "winreg", registry)
    browser = MagicMock()
    browser.windows_force_list_registry_key.return_value = "test-only-policy"
    with patch("src.sync.extension_install.platform.system", return_value="Windows"):
        release_forced_extensions(browser, {EXT_ID})
    assert registry.DeleteValue.call_count == 1
    assert registry.DeleteValue.call_args.args[1] == "1"
    registry.DeleteKey.assert_not_called()


def test_policy_release_is_once_per_browser_and_failure_is_retried(tmp_path):
    _write(tmp_path / "webstore_extensions.json", {EXT_ID: "Example"})
    browser = MagicMock()
    browser.name = "Test"
    browser.ext_id_aliases = {}
    browser.external_extensions_dir.return_value = None
    with (
        patch("src.sync.extension_install.platform.system", return_value="Linux"),
        patch("src.sync.extension_install.release_forced_extensions") as release,
    ):
        release.side_effect = OSError("authorization denied")
        with pytest.raises(OSError, match="authorization denied"):
            install_external_extensions(tmp_path, browser, ungoogled_only_ext_ids=[])
        assert config.get_released_extension_policies() == []
        release.side_effect = None
        install_external_extensions(tmp_path, browser, ungoogled_only_ext_ids=[])
        install_external_extensions(tmp_path, browser, ungoogled_only_ext_ids=[])
        assert release.call_count == 2
    assert config.get_released_extension_policies() == ["Test"]


def _browser(name: str, profile: Path):
    browser = MagicMock()
    browser.name = name
    browser.ext_id_aliases = {}
    browser.is_installed.return_value = True
    browser.is_running.return_value = False
    browser.discover_profiles.return_value = [profile]
    browser.local_state_path.return_value = None
    return browser


def test_global_removal_is_processed_before_any_profiles_can_push_stale_settings(tmp_path):
    sync = tmp_path / "current"
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example"})
    data = Path("Local Extension Settings") / EXT_ID / "data.ldb"
    (sync / data).parent.mkdir(parents=True)
    (sync / data).write_text("saved backup")
    first, second = tmp_path / "first", tmp_path / "second"
    for directory, ids in ((first, {EXT_ID: 1}), (second, {})):
        _profile(directory, ids)
        _write(directory / "Preferences", {})
        config.set_extension_observation(directory, [EXT_ID])
    (first / data).parent.mkdir(parents=True)
    (first / data).write_text("stale browser settings")
    browsers = [_browser("First", first), _browser("Second", second)]
    config.set_enabled_profiles({"First": [first.name], "Second": [second.name]})
    with patch("src.sync_engine.install_external_extensions"):
        SyncEngine(tmp_path, browsers=browsers).sync_all()
    assert extensions.read_library(sync)[EXT_ID]["archived"]
    assert (sync / data).read_text() == "saved backup"


def test_failed_sync_does_not_commit_library_or_observation_changes(tmp_path, monkeypatch):
    sync, profile = tmp_path / "current", tmp_path / "profile"
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example"})
    _profile(profile, {})
    config.set_extension_observation(profile, [EXT_ID])
    config.set_enabled_profiles({"Test": [profile.name]})
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.setattr("src.sync_engine.tempfile.mkdtemp", lambda **kwargs: str(work))
    engine = SyncEngine(tmp_path, browsers=[_browser("Test", profile)])
    with (
        patch.object(engine, "_sync_single_profile", side_effect=OSError("copy failed")),
        pytest.raises(OSError, match="copy failed"),
    ):
        engine.sync_all()
    assert not extensions.read_library(sync)[EXT_ID]["archived"]
    assert config.get_extension_observation(profile) == [EXT_ID]
    assert not work.exists()


def test_restore_never_merges_protected_extension_state_from_legacy_backup(tmp_path):
    sync, profile = tmp_path / "current", tmp_path / "profile"
    local = {"extensions": {"settings": {EXT_ID: {"state": 0}}, "pinned_extensions": []}}
    _write(profile / "Preferences", local)
    _write(
        sync / "preferences.json",
        {
            "extensions": {"settings": {EXT_ID: {"state": 1}}, "pinned_extensions": [EXT_ID]},
            "unknown_old_key": "must not restore",
        },
    )
    SyncEngine(tmp_path, browsers=[]).restore_profile_from_backup(profile, sync)
    restored = json.loads((profile / "Preferences").read_text())
    assert restored["extensions"]["settings"][EXT_ID]["state"] == 0
    assert restored["extensions"]["pinned_extensions"] == [EXT_ID]
    assert "unknown_old_key" not in restored


def test_active_library_is_authoritative_when_manifest_is_outdated(tmp_path):
    _write(tmp_path / "extension_library.json", {EXT_ID: {"name": "Example", "archived": False}})
    _write(tmp_path / "webstore_extensions.json", {})
    assert EXT_ID in extensions.extension_install_ids(tmp_path)


def test_archive_and_restore_publish_new_revisions_for_other_machines(tmp_path):
    extensions.set_archived(tmp_path, {EXT_ID}, True)
    archived_revision = (tmp_path / "metadata.json").read_text()
    extensions.set_archived(tmp_path, {EXT_ID}, True)
    assert (tmp_path / "metadata.json").read_text() == archived_revision
    extensions.set_archived(tmp_path, {EXT_ID}, False)
    assert (tmp_path / "metadata.json").read_text() != archived_revision


def test_large_database_records_are_backed_up_without_rewriting(tmp_path):
    profile, sync = tmp_path / "profile", tmp_path / "sync"
    _profile(profile, {EXT_ID: 1})
    _write(sync / "webstore_extensions.json", {EXT_ID: "Example"})
    payload = b"unchanged database records\x00\xff" * 8000
    relative = Path("Local Extension Settings") / EXT_ID / "000001.log"
    (profile / relative).parent.mkdir(parents=True)
    (profile / relative).write_bytes(payload)
    SyncEngine(tmp_path, browsers=[]).sync_browser_profile(profile, sync, direction="push")
    assert (sync / relative).read_bytes() == payload


def test_restore_with_extensions_disabled_does_not_change_pins(tmp_path):
    sync, profile = tmp_path / "current", tmp_path / "profile"
    _write(profile / "Preferences", {"extensions": {"pinned_extensions": []}})
    _write(sync / "preferences.json", {"extensions": {"pinned_extensions": [EXT_ID]}})
    SyncEngine(tmp_path, browsers=[]).restore_profile_from_backup(
        profile, sync, {"extensions": False}
    )
    assert (
        json.loads((profile / "Preferences").read_text())["extensions"]["pinned_extensions"] == []
    )
