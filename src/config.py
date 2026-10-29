from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from pathlib import Path

from src.operations import operation_lock
from src.sync import write_text_if_changed


def _get_config_dir() -> Path:
    if sys.platform == "win32":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / (
            "chromium-profile-syncer"
        )
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / (
        "chromium-profile-syncer"
    )


CONFIG_DIR = _get_config_dir()
CONFIG_PATH = CONFIG_DIR / "config.json"


def load() -> dict:
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a JSON object in {CONFIG_PATH}")
    return data


def save(data: dict) -> None:
    with operation_lock():
        write_text_if_changed(CONFIG_PATH, json.dumps(data, indent=2))


def mutate(change: Callable[[dict], None]) -> None:
    with operation_lock():
        data = load()
        change(data)
        save(data)


def update(**values) -> None:
    mutate(lambda data: data.update(values))


def _value(key: str, default):
    value = load().get(key, default)
    if not isinstance(value, type(default)):
        raise ValueError(f"Invalid config value for {key}")
    return value


def get_sync_folder() -> Path | None:
    value = load().get("sync_folder")
    return Path(value) if value else None


def set_sync_folder(path: Path | None) -> None:
    update(sync_folder=str(path) if path else None)


def get_enabled_browsers() -> dict[str, bool]:
    return _value("enabled_browsers", {})


def set_enabled_browsers(value: dict[str, bool]) -> None:
    update(enabled_browsers=value)


def get_enabled_profiles() -> dict[str, list[str]]:
    return _value("enabled_profiles", {})


def set_enabled_profiles(value: dict[str, list[str]]) -> None:
    update(enabled_profiles=value)


def get_profile_directions() -> dict[str, dict[str, str]]:
    return _value("profile_directions", {})


def set_profile_directions(value: dict[str, dict[str, str]]) -> None:
    update(profile_directions=value)


def get_autostart() -> bool:
    return _value("autostart", True)


def set_autostart(value: bool) -> None:
    update(autostart=value)


def get_sync_interval() -> int:
    return _value("sync_interval", 15)


def set_sync_interval(value: int) -> None:
    if value < 1:
        raise ValueError("Sync interval must be at least one minute")
    update(sync_interval=value)


def get_profiles_needing_restore() -> dict[str, list[str]]:
    return _value("profiles_needing_restore", {})


def _profile_flag(key: str, browser: str, profile: str, enabled: bool) -> None:
    def change(data):
        bucket = data.setdefault(key, {})
        names = set(bucket.get(browser, []))
        names.add(profile) if enabled else names.discard(profile)
        if names:
            bucket[browser] = sorted(names)
        else:
            bucket.pop(browser, None)

    mutate(change)


def mark_profile_for_restore(browser: str, profile: str) -> None:
    _profile_flag("profiles_needing_restore", browser, profile, True)


def clear_restore_flag(browser: str, profile: str) -> None:
    _profile_flag("profiles_needing_restore", browser, profile, False)


def get_pending_extension_restores() -> dict[str, dict[str, list[str]]]:
    return _value("pending_extension_restores", {})


def request_extension_restore(browser: str, profile: str, ids: set[str]) -> None:
    if not ids:
        return

    def change(data):
        profiles = data.setdefault("pending_extension_restores", {}).setdefault(browser, {})
        profiles[profile] = sorted(set(profiles.get(profile, [])) | ids)

    mutate(change)


def complete_extension_restores(browser: str, profile: str, ids: set[str]) -> None:
    def change(data):
        bucket = data.get("pending_extension_restores", {})
        profiles = bucket.get(browser, {})
        remaining = set(profiles.get(profile, [])) - ids
        if remaining:
            profiles[profile] = sorted(remaining)
        else:
            profiles.pop(profile, None)
        if not profiles:
            bucket.pop(browser, None)

    mutate(change)


def is_profile_sync_enabled(browser: str, profile: str) -> bool:
    return profile not in _value("profile_sync_disabled", {}).get(browser, [])


def set_profile_sync_enabled(browser: str, profile: str, enabled: bool) -> None:
    if not isinstance(browser, str) or not browser or not isinstance(profile, str) or not profile:
        raise TypeError("Browser and profile must be non-empty strings")
    _profile_flag("profile_sync_disabled", browser, profile, not enabled)


def remove_browser_profile(browser: str) -> None:
    def change(data):
        for key in (
            "enabled_profiles",
            "enabled_browsers",
            "profile_sync_disabled",
            "profiles_needing_restore",
            "pending_extension_restores",
            "profile_directions",
        ):
            data.get(key, {}).pop(browser, None)

    mutate(change)


def remove_profile(browser: str, profile: str, path: Path) -> None:
    def change(data):
        for key in ("enabled_profiles", "profile_sync_disabled", "profiles_needing_restore"):
            bucket = data.get(key, {})
            remaining = [name for name in bucket.get(browser, []) if name != profile]
            if remaining:
                bucket[browser] = remaining
            else:
                bucket.pop(browser, None)
        for key in ("profile_directions", "pending_extension_restores"):
            data.get(key, {}).get(browser, {}).pop(profile, None)
        if not data.get("enabled_profiles", {}).get(browser):
            data.get("enabled_browsers", {}).pop(browser, None)
        data.get("extension_observations", {}).pop(str(path.resolve()), None)

    mutate(change)


def get_last_sync() -> str:
    return _value("last_sync", "")


def set_last_sync(value: str) -> None:
    update(last_sync=value)


def get_last_restored_browser() -> str:
    return _value("last_restored_browser", "")


def set_last_restored_browser(value: str) -> None:
    update(last_restored_browser=value)


def get_ungoogled_only_extensions() -> list[str]:
    return _value("ungoogled_only_extensions", [])


def set_ungoogled_only_extensions(value: list[str]) -> None:
    update(ungoogled_only_extensions=value)


def get_windows_only_extensions() -> list[str]:
    return _value("windows_only_extensions", ["jhcpefjbhmbkgjgipkhndplfbhdecijh"])


def set_windows_only_extensions(value: list[str]) -> None:
    update(windows_only_extensions=value)


def get_excluded_ext_settings_ids() -> list[str]:
    return _value(
        "excluded_ext_settings_ids",
        [
            "eimadpbcbfnmbkopoojfekhnkhdbieeh",
            "jnpglhiolmmfchhpoipnknmffmpmogmc",
        ],
    )


def set_excluded_ext_settings_ids(value: list[str]) -> None:
    update(excluded_ext_settings_ids=value)


def get_flags_ignore() -> list[str]:
    return _value("flags_ignore", [])


def set_flags_ignore(value: list[str]) -> None:
    update(flags_ignore=sorted(set(value)))


def get_extension_observation(profile: Path) -> list[str] | None:
    return _value("extension_observations", {}).get(str(profile.resolve()))


def set_extension_observation(profile: Path, ids: list[str]) -> None:
    mutate(
        lambda data: data.setdefault("extension_observations", {}).update(
            {str(profile.resolve()): ids}
        )
    )


def get_released_extension_policies() -> list[str]:
    return _value("released_extension_policies", [])


def mark_extension_policies_released(browser: str) -> None:
    mutate(
        lambda data: data.update(
            released_extension_policies=sorted(
                set(data.get("released_extension_policies", [])) | {browser}
            )
        )
    )
