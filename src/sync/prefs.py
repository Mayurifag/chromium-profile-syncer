from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path

from src.sync import _noop

_LOG = logging.getLogger(__name__)

PREFERENCES_KEYS: tuple[str, ...] = (
    "enable_do_not_track",
    "https_only_mode_enabled",
    "safe_browsing",
    "safebrowsing",
    "net.network_prediction_options",
    "credentials_enable_service",
    "credentials_enable_autosignin",
    "intl",
    "spellcheck",
    "translate_blocked_languages",
    "translate_allowlists",
    "translate_recent_target",
    "search",
    "omnibox",
    "toolbar",
    "bookmark_bar",
    "custom_links",
    "extensions.pinned_extensions",
    "session.restore_on_startup",
    "session.startup_urls",
    "homepage",
    "homepage_is_newtabpage",
    "browser.enable_spellchecking",
    "browser.theme",
    "savefile",
    "selectfile",
    "devtools.preferences",
    "partition.per_host_zoom_levels",
    "custom_handlers",
    "profile.content_settings.exceptions.geolocation",
    "profile.content_settings.exceptions.notifications",
    "profile.content_settings.exceptions.media_stream_mic",
    "profile.content_settings.exceptions.media_stream_camera",
    "profile.content_settings.exceptions.popups",
    "profile.content_settings.exceptions.http_allowed",
    "profile.content_settings.exceptions.javascript",
    "profile.content_settings.exceptions.cookies",
    "profile.content_settings.exceptions.sound",
    "profile.content_settings.exceptions.autoplay",
    "profile.content_settings.exceptions.automatic_downloads",
    "profile.content_settings.exceptions.window_placement",
    "profile.content_settings.exceptions.hid_chooser_data",
    "profile.content_settings.exceptions.ssl_cert_decisions",
    "profile.content_settings.exceptions.fedcm_idp_signin",
)


def _get_nested(d: dict, keys: list[str]) -> tuple[bool, object]:
    cur: object = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return False, None
        cur = cur[k]
    return True, cur


def _set_nested(d: dict, keys: list[str], value: object) -> None:
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value


def merge_prefs(target: dict, source: dict) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            merge_prefs(target[key], value)
        else:
            target[key] = value


def sync_preferences_json(
    profile_path: Path,
    sync_profile_path: Path,
    direction: str,
    report: Callable[[str], None] = _noop,
) -> tuple[int, int]:
    prefs_path = profile_path / "Preferences"
    json_path = sync_profile_path / "preferences.json"

    local_mtime = prefs_path.stat().st_mtime if prefs_path.exists() else 0.0
    remote_mtime = json_path.stat().st_mtime if json_path.exists() else 0.0

    do_push = direction == "push" or (direction == "both" and local_mtime > remote_mtime)
    do_pull = direction == "pull" or (direction == "both" and remote_mtime > local_mtime)

    if do_push and prefs_path.exists():
        prefs = json.loads(prefs_path.read_bytes())
        extracted: dict = {}
        for dotted in PREFERENCES_KEYS:
            keys = dotted.split(".")
            found, value = _get_nested(prefs, keys)
            if found:
                _set_nested(extracted, keys, value)
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps(extracted), encoding="utf-8")
        report("preferences.json")
        return 1, 0

    if do_pull and json_path.exists() and prefs_path.exists():
        saved = json.loads(json_path.read_bytes())
        prefs = json.loads(prefs_path.read_bytes())
        merge_prefs(prefs, saved)
        prefs_path.write_bytes(json.dumps(prefs, separators=(",", ":")).encode())
        report("Preferences")
        return 1, 0

    return 0, 1
