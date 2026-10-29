from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from src.browsers import ALL_BROWSERS, get_browser


def test_browser_specs_have_unique_names():
    assert {browser.name for browser in ALL_BROWSERS} == {"Chrome", "Helium", "Thorium", "Yandex"}
    assert get_browser("helium").ungoogled
    assert get_browser("missing") is None


@pytest.mark.parametrize("browser", ALL_BROWSERS, ids=lambda browser: browser.name)
def test_profile_paths_and_discovery(browser, tmp_path):
    assert isinstance(browser.profile_root(), Path)
    for name in ("Default", "Profile 1", "Guest Profile", "Cache"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "Preferences").write_text("{}")
    (tmp_path / "Profile 2").mkdir()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        assert browser.is_installed()
        assert {p.name for p in browser.discover_profiles()} == {"Default", "Profile 1"}
        assert browser.external_extensions_dir() == tmp_path / "External Extensions"
        assert browser.local_state_path() == tmp_path / "Local State"


@pytest.mark.parametrize("browser", ALL_BROWSERS, ids=lambda browser: browser.name)
def test_missing_browser_and_process(browser, tmp_path):
    with patch.object(type(browser), "profile_root", return_value=tmp_path / "missing"):
        assert not browser.is_installed()
        assert browser.discover_profiles() == []
    assert not browser.is_running(set())


@pytest.mark.parametrize(
    "info, expected",
    [
        ({"name": "Work", "is_using_default_name": False}, "Work"),
        ({"user_name": "user@example.invalid"}, "user@example.invalid"),
        ({"gaia_name": "Person"}, "Person"),
        ({}, "Profile name"),
    ],
)
def test_profile_display_name(info, expected, tmp_path):
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Preferences").write_text(json.dumps({"profile": {"name": "Profile name"}}))
    (tmp_path / "Local State").write_text(
        json.dumps({"profile": {"info_cache": {"Default": info}}})
    )
    browser = get_browser("Chrome")
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        assert browser.get_profile_name(profile) == expected


def test_system_wide_windows_chrome_executable(tmp_path, monkeypatch):
    browser = get_browser("Chrome")
    binary = tmp_path / "programs" / "Google" / "Chrome" / "Application" / "chrome.exe"
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setenv("PROGRAMFILES", str(tmp_path / "programs"))
    with (
        patch("src.browsers.base.sys.platform", "win32"),
        patch.object(type(browser), "profile_root", return_value=tmp_path / "user" / "User Data"),
    ):
        assert browser.executable() == binary
