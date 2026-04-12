from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from src.browsers import ALL_BROWSERS
from src.browsers.chrome import Chrome
from src.browsers.helium import Helium
from src.browsers.thorium import Thorium
from src.browsers.yandex import Yandex


def test_browser_names() -> None:
    assert Thorium().name == "Thorium"
    assert Helium().name == "Helium"
    assert Chrome().name == "Chrome"
    assert Yandex().name == "Yandex"


def test_all_browsers_list() -> None:
    assert len(ALL_BROWSERS) == 4
    types = {type(b) for b in ALL_BROWSERS}
    assert Thorium in types
    assert Helium in types
    assert Chrome in types
    assert Yandex in types


def test_profile_root_returns_path() -> None:
    for browser in (Thorium(), Helium(), Chrome(), Yandex()):
        result = browser.profile_root()
        assert isinstance(result, Path)


def test_is_installed_false_when_missing() -> None:
    for cls in (Thorium, Helium, Chrome, Yandex):
        browser = cls()
        missing = Path("/nonexistent/does/not/exist")
        with patch.object(type(browser), "profile_root", return_value=missing):
            assert browser.is_installed() is False


def test_is_installed_true_when_exists(tmp_path: Path) -> None:
    for cls in (Thorium, Helium, Chrome, Yandex):
        browser = cls()
        with patch.object(type(browser), "profile_root", return_value=tmp_path):
            assert browser.is_installed() is True


def test_discover_profiles_finds_default_and_numbered(tmp_path: Path) -> None:
    for name in ("Default", "Profile 1", "Profile 2"):
        d = tmp_path / name
        d.mkdir()
        (d / "Preferences").write_text("{}")

    browser = Thorium()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        profiles = browser.discover_profiles()

    assert len(profiles) == 3
    names = [p.name for p in profiles]
    assert "Default" in names
    assert "Profile 1" in names
    assert "Profile 2" in names


def test_discover_profiles_skips_dirs_without_preferences(tmp_path: Path) -> None:
    (tmp_path / "Default").mkdir()  # no Preferences file
    profile1 = tmp_path / "Profile 1"
    profile1.mkdir()
    (profile1 / "Preferences").write_text("{}")

    browser = Thorium()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        profiles = browser.discover_profiles()

    assert len(profiles) == 1
    assert profiles[0].name == "Profile 1"


def test_discover_profiles_skips_non_matching_dirs(tmp_path: Path) -> None:
    for name in ("Guest Profile", "System Profile", "Cache"):
        d = tmp_path / name
        d.mkdir()
        (d / "Preferences").write_text("{}")

    browser = Thorium()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        profiles = browser.discover_profiles()

    assert profiles == []


def test_discover_profiles_empty_when_not_installed() -> None:
    browser = Thorium()
    missing = Path("/nonexistent/does/not/exist")
    with patch.object(type(browser), "profile_root", return_value=missing):
        assert browser.discover_profiles() == []


def test_external_extensions_dir_returns_path(tmp_path: Path) -> None:
    browser = Yandex()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        result = browser.external_extensions_dir()
    assert result == tmp_path / "External Extensions"


def test_is_running_false_when_no_process() -> None:
    import psutil

    with patch.object(psutil, "process_iter", return_value=[]):
        for cls in (Thorium, Helium, Chrome, Yandex):
            assert cls().is_running() is False


def test_get_profile_name_reads_preferences(tmp_path: Path) -> None:
    profile_dir = tmp_path / "Profile 1"
    profile_dir.mkdir()
    (profile_dir / "Preferences").write_text(
        json.dumps({"profile": {"name": "Work"}}), encoding="utf-8"
    )
    browser = Chrome()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        assert browser.get_profile_name(profile_dir) == "Work"


def test_get_profile_name_fallback_to_dir_name(tmp_path: Path) -> None:
    browser = Chrome()
    with patch.object(type(browser), "profile_root", return_value=tmp_path.parent):
        assert browser.get_profile_name(tmp_path) == tmp_path.name


def test_get_profile_name_prefers_custom_name_from_local_state(tmp_path: Path) -> None:
    profile_dir = tmp_path / "Profile 1"
    profile_dir.mkdir()
    (tmp_path / "Local State").write_text(
        json.dumps({
            "profile": {
                "info_cache": {
                    "Profile 1": {
                        "name": "Crypto",
                        "user_name": "test@example.com",
                        "gaia_name": "Test User",
                        "is_using_default_name": False,
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    browser = Chrome()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        assert browser.get_profile_name(profile_dir) == "Crypto"


def test_get_profile_name_uses_email_when_no_custom_name(tmp_path: Path) -> None:
    profile_dir = tmp_path / "Profile 2"
    profile_dir.mkdir()
    (tmp_path / "Local State").write_text(
        json.dumps({
            "profile": {
                "info_cache": {
                    "Profile 2": {
                        "name": "Person 1",
                        "user_name": "work@example.com",
                        "gaia_name": "John Doe",
                        "is_using_default_name": True,
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    browser = Chrome()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        assert browser.get_profile_name(profile_dir) == "work@example.com"


def test_get_profile_name_uses_gaia_name_when_no_email_or_custom(tmp_path: Path) -> None:
    profile_dir = tmp_path / "Profile 3"
    profile_dir.mkdir()
    (tmp_path / "Local State").write_text(
        json.dumps({
            "profile": {
                "info_cache": {
                    "Profile 3": {
                        "name": "Person 1",
                        "user_name": "",
                        "gaia_name": "John Doe",
                        "is_using_default_name": True,
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    browser = Chrome()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        assert browser.get_profile_name(profile_dir) == "John Doe"


def test_get_profile_name_skips_default_name(tmp_path: Path) -> None:
    profile_dir = tmp_path / "Profile 4"
    profile_dir.mkdir()
    (profile_dir / "Preferences").write_text(
        json.dumps({"profile": {"name": "MyProfile"}}), encoding="utf-8"
    )
    (tmp_path / "Local State").write_text(
        json.dumps({
            "profile": {
                "info_cache": {
                    "Profile 4": {
                        "name": "Person 1",
                        "user_name": "",
                        "gaia_name": "",
                        "is_using_default_name": True,
                    }
                }
            }
        }),
        encoding="utf-8",
    )
    browser = Chrome()
    with patch.object(type(browser), "profile_root", return_value=tmp_path):
        # Should fall back to Preferences since Local State name is default
        assert browser.get_profile_name(profile_dir) == "MyProfile"
