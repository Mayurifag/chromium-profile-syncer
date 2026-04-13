from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

_LOG = logging.getLogger(__name__)


def _get_config_dir() -> Path:
    """Return platform-specific config directory."""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / "chromium-profile-syncer"
        return Path.home() / "AppData" / "Roaming" / "chromium-profile-syncer"
    # Unix: use XDG_CONFIG_HOME or ~/.config
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        return Path(xdg_config) / "chromium-profile-syncer"
    return Path.home() / ".config" / "chromium-profile-syncer"


CONFIG_DIR: Path = _get_config_dir()
CONFIG_PATH: Path = CONFIG_DIR / "config.json"


def load() -> dict:
    """Read and parse config JSON. Returns {} if missing or corrupt."""
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError):
        _LOG.warning("Could not read config at %s — returning empty config", CONFIG_PATH)
        return {}


def save(data: dict) -> None:
    """Write data to config JSON, creating directories as needed."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _LOG.debug("Config saved to %s", CONFIG_PATH)


def get_sync_folder() -> Path | None:
    """Return the configured sync folder as a Path, or None if not set."""
    value = load().get("sync_folder")
    return Path(value) if value else None


def set_sync_folder(p: Path | None) -> None:
    """Persist the sync folder path in config. Pass None to clear."""
    data = load()
    if p is None:
        data.pop("sync_folder", None)
        _LOG.info("sync_folder cleared")
    else:
        data["sync_folder"] = str(p)
        _LOG.info("sync_folder set to %s", p)
    save(data)


def get_enabled_browsers() -> dict[str, bool]:
    """Return {browser_name: enabled} dict. Empty dict means all installed are enabled."""
    return load().get("enabled_browsers", {})


def set_enabled_browsers(browsers: dict[str, bool]) -> None:
    """Persist enabled_browsers mapping."""
    data = load()
    data["enabled_browsers"] = browsers
    save(data)
    _LOG.info("enabled_browsers updated: %s", browsers)


def get_enabled_profiles() -> dict[str, list[str]]:
    """Return {browser_name: [profile_names]} for enabled profiles."""
    return load().get("enabled_profiles", {})


def set_enabled_profiles(profiles: dict[str, list[str]]) -> None:
    """Persist enabled_profiles mapping."""
    data = load()
    data["enabled_profiles"] = profiles
    save(data)
    _LOG.info("enabled_profiles updated")


def get_profile_directions() -> dict[str, dict[str, str]]:
    """Return {browser_name: {profile_name: direction}}.
    direction is one of: "both", "push", "pull". Default "both".
    """
    return load().get("profile_directions", {})


def set_profile_directions(directions: dict[str, dict[str, str]]) -> None:
    data = load()
    data["profile_directions"] = directions
    save(data)


def get_autostart() -> bool:
    """Return whether the app should start on login. Defaults to True."""
    return load().get("autostart", True)


def set_autostart(enabled: bool) -> None:
    """Persist the autostart setting."""
    data = load()
    data["autostart"] = enabled
    save(data)
    _LOG.info("autostart set to %s", enabled)


def get_sync_interval() -> int:
    """Return sync interval in minutes. Defaults to 15."""
    return load().get("sync_interval", 15)


def set_sync_interval(minutes: int) -> None:
    """Persist the sync interval setting."""
    data = load()
    data["sync_interval"] = minutes
    save(data)
    _LOG.info("sync_interval set to %d minutes", minutes)


def get_profiles_needing_restore() -> dict[str, list[str]]:
    """Return {browser_name: [profile_names]} for profiles that need initial restore from backup."""
    return load().get("profiles_needing_restore", {})


def mark_profile_for_restore(browser: str, profile: str) -> None:
    """Mark a profile to be restored from backup on next sync."""
    data = load()
    if "profiles_needing_restore" not in data:
        data["profiles_needing_restore"] = {}
    if browser not in data["profiles_needing_restore"]:
        data["profiles_needing_restore"][browser] = []
    if profile not in data["profiles_needing_restore"][browser]:
        data["profiles_needing_restore"][browser].append(profile)
        save(data)
        _LOG.info("Marked %s/%s for initial restore", browser, profile)


def clear_restore_flag(browser: str, profile: str) -> None:
    """Clear the restore flag after initial restore is complete."""
    data = load()
    if "profiles_needing_restore" not in data:
        return
    if browser not in data["profiles_needing_restore"]:
        return
    if profile in data["profiles_needing_restore"][browser]:
        data["profiles_needing_restore"][browser].remove(profile)
        if not data["profiles_needing_restore"][browser]:
            del data["profiles_needing_restore"][browser]
        save(data)
        _LOG.info("Cleared restore flag for %s/%s", browser, profile)


def get_ungoogled_only_extensions() -> list[str]:
    """Return extension IDs that should only be installed in ungoogled browsers.

    These extensions compensate for features that regular Chromium builds provide
    natively (e.g. translation). Installing them in Google Chrome or other browsers
    with those features built in would be redundant.
    """
    return load().get("ungoogled_only_extensions", [])


def set_ungoogled_only_extensions(ext_ids: list[str]) -> None:
    """Persist the list of ungoogled-only extension IDs."""
    data = load()
    data["ungoogled_only_extensions"] = ext_ids
    save(data)
    _LOG.info("ungoogled_only_extensions updated: %s", ext_ids)


