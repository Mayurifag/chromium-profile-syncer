from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.parametrize("tray_only", [False, True])
def test_startup_respects_tray_and_autostart_preferences(tray_only):
    from src import main

    with (
        patch.object(sys, "argv", ["app", *(["--tray"] if tray_only else [])]),
        patch.object(main, "QApplication") as application,
        patch.object(main, "SingleInstance") as instance,
        patch.object(main, "TrayApp"),
        patch.object(main, "make_app_icon"),
        patch.object(main, "QTimer") as timer,
        patch.object(main.autostart, "apply") as autostart,
        patch.object(main.config, "get_autostart", return_value=False),
        patch.object(main.config, "get_enabled_profiles", return_value={"Chrome": ["Default"]}),
    ):
        application.return_value.exec.return_value = 0
        instance.return_value.acquire.return_value = True
        with pytest.raises(SystemExit):
            main.main()
        autostart.assert_called_once_with(False)
        assert timer.singleShot.call_count == (0 if tray_only else 1)


def test_main_routes_cli_to_the_same_entry_point():
    from src import main

    with (
        patch.object(sys, "argv", ["app", "sync", "--browser", "Chrome"]),
        patch.object(main.cli, "run", return_value=0) as run,
        pytest.raises(SystemExit),
    ):
        main.main()
    run.assert_called_once_with(["sync", "--browser", "Chrome"])


def test_remove_one_profile_preserves_shared_backup(tmp_path, monkeypatch, capsys):
    from src import cli, config

    profile = tmp_path / "Default"
    profile.mkdir()
    backup = tmp_path / "current"
    backup.mkdir()
    (backup / "Bookmarks").write_text("shared backup")
    browser = MagicMock()
    browser.name = "Browser"
    browser.is_running.return_value = False
    browser.discover_profiles.return_value = [profile]
    monkeypatch.setattr(cli, "get_browser", lambda name: browser)
    config.set_sync_folder(backup.parent)
    assert cli.run(["remove-profile", "--browser", "Browser", "--profile", "Default", "--yes"]) == 0
    assert not profile.exists()
    assert (backup / "Bookmarks").read_text() == "shared backup"
