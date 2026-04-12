from __future__ import annotations

import json
import logging
import os
from pathlib import Path

_LOG = logging.getLogger(__name__)

CONFIG_DIR: Path = (
    Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    / "chromium-profile-syncer"
)
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


def set_sync_folder(p: Path) -> None:
    """Persist the sync folder path in config."""
    data = load()
    data["sync_folder"] = str(p)
    save(data)
    _LOG.info("sync_folder set to %s", p)


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


