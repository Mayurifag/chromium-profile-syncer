from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path

_LOG = logging.getLogger(__name__)


def _get_config_dir() -> Path:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "chromium-profile-syncer"
        return Path.home() / "AppData" / "Roaming" / "chromium-profile-syncer"
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "chromium-profile-syncer"
    return Path.home() / ".config" / "chromium-profile-syncer"


CONFIG_DIR: Path = _get_config_dir()
CONFIG_PATH: Path = CONFIG_DIR / "config.json"

_data: dict | None = None
_loaded_from: Path | None = None
_lock = threading.RLock()


def _get() -> dict:
    global _data, _loaded_from
    with _lock:
        if _data is None or _loaded_from != CONFIG_PATH:
            _loaded_from = CONFIG_PATH
            try:
                _data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            except FileNotFoundError:
                _data = {}
            except (json.JSONDecodeError, OSError):
                _LOG.warning("Could not read config at %s — returning empty config", CONFIG_PATH)
                _data = {}
        return _data


def _flush() -> None:
    global _loaded_from
    with _lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(_data, indent=2), encoding="utf-8")
        _loaded_from = CONFIG_PATH
        _LOG.debug("Config saved to %s", CONFIG_PATH)


def _mark_profile_state(key: str, browser: str, profile: str, log_msg: str) -> None:
    with _lock:
        data = _get()
        bucket = data.setdefault(key, {})
        profiles: list[str] = bucket.setdefault(browser, [])
        if profile not in profiles:
            profiles.append(profile)
            _flush()
            _LOG.info(log_msg, browser, profile)


def _clear_profile_state(key: str, browser: str, profile: str, log_msg: str) -> None:
    with _lock:
        data = _get()
        bucket = data.get(key, {})
        profiles = bucket.get(browser, [])
        if profile in profiles:
            profiles.remove(profile)
            if not profiles:
                del bucket[browser]
            _flush()
            _LOG.info(log_msg, browser, profile)


def load() -> dict:
    return dict(_get())


def save(data: dict) -> None:
    global _data
    _data = data
    _flush()


def get_sync_folder() -> Path | None:
    value = _get().get("sync_folder")
    return Path(value) if value else None


def set_sync_folder(p: Path | None) -> None:
    if p is None:
        _get().pop("sync_folder", None)
        _LOG.info("sync_folder cleared")
    else:
        _get()["sync_folder"] = str(p)
        _LOG.info("sync_folder set to %s", p)
    _flush()


def get_enabled_browsers() -> dict[str, bool]:
    return _get().get("enabled_browsers", {})


def set_enabled_browsers(browsers: dict[str, bool]) -> None:
    _get()["enabled_browsers"] = browsers
    _flush()
    _LOG.info("enabled_browsers updated: %s", browsers)


def get_enabled_profiles() -> dict[str, list[str]]:
    return _get().get("enabled_profiles", {})


def set_enabled_profiles(profiles: dict[str, list[str]]) -> None:
    _get()["enabled_profiles"] = profiles
    _flush()
    _LOG.info("enabled_profiles updated")


def get_profile_directions() -> dict[str, dict[str, str]]:
    return _get().get("profile_directions", {})


def set_profile_directions(directions: dict[str, dict[str, str]]) -> None:
    _get()["profile_directions"] = directions
    _flush()


def get_autostart() -> bool:
    return _get().get("autostart", True)


def set_autostart(enabled: bool) -> None:
    _get()["autostart"] = enabled
    _flush()
    _LOG.info("autostart set to %s", enabled)


def get_sync_interval() -> int:
    return _get().get("sync_interval", 15)


def set_sync_interval(minutes: int) -> None:
    _get()["sync_interval"] = minutes
    _flush()
    _LOG.info("sync_interval set to %d minutes", minutes)


def get_profiles_needing_restore() -> dict[str, list[str]]:
    return _get().get("profiles_needing_restore", {})


def mark_profile_for_restore(browser: str, profile: str) -> None:
    _mark_profile_state(
        "profiles_needing_restore", browser, profile,
        "Marked %s/%s for initial restore",
    )


def clear_restore_flag(browser: str, profile: str) -> None:
    _clear_profile_state(
        "profiles_needing_restore", browser, profile,
        "Cleared restore flag for %s/%s",
    )


def get_profiles_needing_ext_repull() -> dict[str, list[str]]:
    return _get().get("profiles_needing_ext_repull", {})


def mark_profile_for_ext_repull(browser: str, profile: str) -> None:
    _mark_profile_state(
        "profiles_needing_ext_repull", browser, profile,
        "Marked %s/%s for post-install ext repull",
    )


def clear_ext_repull_flag(browser: str, profile: str) -> None:
    _clear_profile_state(
        "profiles_needing_ext_repull", browser, profile,
        "Cleared ext repull flag for %s/%s",
    )


def is_profile_sync_enabled(browser: str, profile: str) -> bool:
    disabled = _get().get("profile_sync_disabled", {})
    return profile not in disabled.get(browser, [])


def set_profile_sync_enabled(browser: str, profile: str, enabled: bool) -> None:
    if not isinstance(browser, str) or not browser:
        raise TypeError(f"browser must be a non-empty str, got {browser!r}")
    if not isinstance(profile, str) or not profile:
        raise TypeError(f"profile must be a non-empty str, got {profile!r}")
    with _lock:
        data = _get()
        disabled = data.setdefault("profile_sync_disabled", {})
        profiles: list = disabled.setdefault(browser, [])
        if not enabled and profile not in profiles:
            profiles.append(profile)
            _flush()
            _LOG.info("Auto-sync disabled for %s/%s", browser, profile)
        elif enabled and profile in profiles:
            profiles.remove(profile)
            if not profiles:
                del disabled[browser]
            _flush()
            _LOG.info("Auto-sync enabled for %s/%s", browser, profile)


def remove_browser_profile(browser: str) -> None:
    with _lock:
        data = _get()
        for key in ("enabled_profiles", "enabled_browsers", "profile_sync_disabled",
                    "profiles_needing_restore", "profiles_needing_ext_repull",
                    "profile_directions"):
            data.get(key, {}).pop(browser, None)
        _flush()
        _LOG.info("Removed %s from config", browser)


def get_last_sync() -> str:
    return _get().get("last_sync", "")


def set_last_sync(ts: str) -> None:
    _get()["last_sync"] = ts
    _flush()


def get_last_restored_browser() -> str:
    return _get().get("last_restored_browser", "")


def set_last_restored_browser(name: str) -> None:
    _get()["last_restored_browser"] = name
    _flush()
    _LOG.info("last_restored_browser set to %s", name)


def get_ungoogled_only_extensions() -> list[str]:
    return _get().get("ungoogled_only_extensions", [])


def set_ungoogled_only_extensions(ext_ids: list[str]) -> None:
    _get()["ungoogled_only_extensions"] = ext_ids
    _flush()
    _LOG.info("ungoogled_only_extensions updated: %s", ext_ids)


_DEFAULT_WINDOWS_ONLY_EXTENSIONS: list[str] = [
    "jhcpefjbhmbkgjgipkhndplfbhdecijh",  # Country Flag Fixer (Windows-only emoji rendering)
]


def get_windows_only_extensions() -> list[str]:
    return _get().get("windows_only_extensions", list(_DEFAULT_WINDOWS_ONLY_EXTENSIONS))


def set_windows_only_extensions(ext_ids: list[str]) -> None:
    _get()["windows_only_extensions"] = ext_ids
    _flush()
    _LOG.info("windows_only_extensions updated: %s", ext_ids)


_DEFAULT_EXCLUDED_EXT_SETTINGS: list[str] = [
    "eimadpbcbfnmbkopoojfekhnkhdbieeh",  # Dark Reader (Newsmaker cache; theme in Sync)
    "jnpglhiolmmfchhpoipnknmffmpmogmc",  # Twitter location cache helper
]


def get_excluded_ext_settings_ids() -> list[str]:
    return _get().get("excluded_ext_settings_ids", list(_DEFAULT_EXCLUDED_EXT_SETTINGS))


def set_excluded_ext_settings_ids(ext_ids: list[str]) -> None:
    _get()["excluded_ext_settings_ids"] = ext_ids
    _flush()
    _LOG.info("excluded_ext_settings_ids updated: %s", ext_ids)


def get_helium_auto_update() -> bool:
    return _get().get("helium_auto_update", True)


def set_helium_auto_update(enabled: bool) -> None:
    _get()["helium_auto_update"] = enabled
    _flush()
    _LOG.info("helium_auto_update set to %s", enabled)


def get_flags_ignore() -> list[str]:
    return _get().get("flags_ignore", [])


def set_flags_ignore(flags: list[str]) -> None:
    _get()["flags_ignore"] = sorted(set(flags))
    _flush()
    _LOG.info("flags_ignore updated: %d entries", len(flags))


