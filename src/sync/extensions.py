from __future__ import annotations

import json
import logging
import platform
import plistlib
import shlex
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.sync import _noop, write_text_if_changed
from src.sync.leveldb import copy_atomic

if TYPE_CHECKING:
    from src.browsers.base import BrowserBase

_LOG = logging.getLogger(__name__)


def _winreg_enum(fn: Callable[..., Any], key: Any) -> list:
    names, i = [], 0
    while True:
        try:
            names.append(fn(key, i))
            i += 1
        except OSError:
            return names


def _winreg_enum_subkeys(key) -> list[str]:
    import winreg

    return _winreg_enum(winreg.EnumKey, key)


def _winreg_enum_values(key) -> list[str]:
    import winreg

    return _winreg_enum(lambda k, i: winreg.EnumValue(k, i)[0], key)


def _parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(x) for x in v.split("."))
    except (ValueError, AttributeError):
        return (0,)


def _is_webstore_extension(version_dir: Path) -> bool:
    if (version_dir / "_metadata" / "verified_contents.json").exists():
        return True
    manifest = version_dir / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            return bool(data.get("update_url"))
        except (OSError, json.JSONDecodeError):
            pass
    return False


def _best_version_dir(id_dir: Path) -> Path | None:
    if not id_dir.exists():
        return None
    dirs = [d for d in id_dir.iterdir() if d.is_dir()]
    return max(dirs, key=_dir_version, default=None)


def _dir_version(version_dir: Path) -> tuple[int, ...]:
    manifest = version_dir / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            ver_str = data.get("version", "")
            if ver_str:
                parsed = _parse_version(str(ver_str))
                if parsed != (0,) or ver_str == "0":
                    return parsed
        except (OSError, json.JSONDecodeError, ValueError):
            pass
    raw_name = version_dir.name.split("_")[0]
    return _parse_version(raw_name)


def _resolve_msg(version_dir: Path, msg_key: str) -> str:
    key = msg_key.removeprefix("__MSG_").removesuffix("__").lower()
    locales_dir = version_dir / "_locales"
    if not locales_dir.exists():
        return ""

    def _read_locale(locale_dir: Path) -> str:
        messages_file = locale_dir / "messages.json"
        if not messages_file.exists():
            return ""
        try:
            messages = json.loads(messages_file.read_text(encoding="utf-8"))
            for k, v in messages.items():
                if k.lower() == key:
                    return v.get("message", "")
        except (OSError, json.JSONDecodeError):
            pass
        return ""

    for locale in ("en", "en_US", "en_GB", "ru_RU"):
        result = _read_locale(locales_dir / locale)
        if result:
            return result

    for locale_dir in locales_dir.iterdir():
        result = _read_locale(locale_dir)
        if result:
            return result

    return ""


def _extension_name(version_dir: Path) -> str:
    manifest = version_dir / "manifest.json"
    if manifest.exists():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            name = data.get("name", "")
            if name:
                if not name.startswith("__MSG_"):
                    return name
                resolved = _resolve_msg(version_dir, name)
                if resolved:
                    return resolved
        except (OSError, json.JSONDecodeError):
            pass
    return version_dir.parent.name


def _sync_extension_id(
    profile_id_dir: Path,
    sync_id_dir: Path,
    ext_id: str,
    direction: str,
    webstore_ids: set[str],
    report: Callable[[str], None],
) -> tuple[int, int]:
    profile_best = _best_version_dir(profile_id_dir)
    sync_best = _best_version_dir(sync_id_dir)

    if profile_best is None and sync_best is None:
        return 0, 0

    check_dir = profile_best or sync_best
    if check_dir and _is_webstore_extension(check_dir):
        webstore_ids.add(ext_id)

    profile_ver = _dir_version(profile_best) if profile_best else (0,)
    sync_ver = _dir_version(sync_best) if sync_best else (0,)

    if profile_ver == sync_ver:
        return 0, 1

    if profile_ver > sync_ver:
        if direction in ("push", "both") and profile_best is not None:
            dest = sync_id_dir / profile_best.name
            ext_name = _extension_name(profile_best)
            _LOG.info(
                "Extension %s (%s): profile %s > sync %s — syncing",
                ext_name,
                ext_id,
                profile_ver,
                sync_ver,
            )
            sync_id_dir.mkdir(parents=True, exist_ok=True)
            copy_atomic(profile_best, dest, report, display_name=ext_name)
            return 1, 0
        return 0, 1
    else:
        if direction in ("pull", "both") and sync_best is not None:
            dest = profile_id_dir / sync_best.name
            ext_name = _extension_name(sync_best)
            _LOG.info(
                "Extension %s (%s): sync %s > profile %s — syncing",
                ext_name,
                ext_id,
                sync_ver,
                profile_ver,
            )
            profile_id_dir.mkdir(parents=True, exist_ok=True)
            copy_atomic(sync_best, dest, report, display_name=ext_name)
            return 1, 0
        return 0, 1


def sync_extensions(
    profile_dir: Path,
    sync_dir: Path,
    direction: str = "both",
    report: Callable[[str], None] = _noop,
) -> tuple[int, int]:
    profile_ext_dir = profile_dir / "Extensions"
    sync_ext_dir = sync_dir / "Extensions"

    ext_ids: set[str] = set()
    if profile_ext_dir.exists():
        ext_ids.update(d.name for d in profile_ext_dir.iterdir() if d.is_dir())
    if sync_ext_dir.exists():
        ext_ids.update(d.name for d in sync_ext_dir.iterdir() if d.is_dir())

    manifest_path = sync_dir / "webstore_extensions.json"
    webstore_ids: set[str] = set()
    if manifest_path.exists():
        try:
            webstore_ids = set(json.loads(manifest_path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass

    total_synced = 0
    total_skipped = 0

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {
            pool.submit(
                _sync_extension_id,
                profile_ext_dir / ext_id,
                sync_ext_dir / ext_id,
                ext_id,
                direction,
                webstore_ids,
                report,
            ): ext_id
            for ext_id in ext_ids
        }
        for fut in as_completed(futures):
            s, sk = fut.result()
            total_synced += s
            total_skipped += sk

    if webstore_ids:
        if write_text_if_changed(manifest_path, json.dumps(sorted(webstore_ids))):
            _LOG.info("Detected %d Web Store extensions (tracking by ID)", len(webstore_ids))

    return total_synced, total_skipped


def update_webstore_manifest(
    profile_dir: Path, sync_dir: Path, aliases: dict[str, str] | None = None
) -> None:
    profile_ext_dir = profile_dir / "Extensions"
    manifest_path = sync_dir / "webstore_extensions.json"
    existing: dict[str, str] = {}
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing = data if isinstance(data, dict) else {e: "" for e in data}
        except (json.JSONDecodeError, OSError):
            pass
    webstore_map: dict[str, str] = {}
    if profile_ext_dir.exists():
        for id_dir in profile_ext_dir.iterdir():
            if not id_dir.is_dir():
                continue
            ext_id = id_dir.name
            canonical = (aliases or {}).get(ext_id, ext_id)
            best = _best_version_dir(id_dir)
            if best and _is_webstore_extension(best):
                name = _extension_name(best)
                webstore_map[canonical] = (
                    name if name != ext_id else existing.get(canonical, ext_id)
                )
            elif canonical != ext_id:
                name = _extension_name(best) if best else ""
                if not name or name == ext_id:
                    name = existing.get(canonical, "")
                webstore_map[canonical] = name
    if aliases:
        data_ids = _profile_extension_data_ids(profile_dir)
        for alias_id, canonical in aliases.items():
            if alias_id in data_ids and canonical not in webstore_map:
                webstore_map[canonical] = existing.get(canonical, "")
    if not webstore_map and existing:
        _LOG.info(
            "Profile has 0 web-store extensions; preserving %d existing manifest entries",
            len(existing),
        )
        return
    if existing:
        webstore_map = existing | webstore_map
    if write_text_if_changed(manifest_path, json.dumps(webstore_map, sort_keys=True)):
        _LOG.info("Updated webstore manifest: %d extensions", len(webstore_map))


def collect_webstore_extensions(profile_dir: Path) -> dict[str, str]:
    ext_dir = profile_dir / "Extensions"
    result: dict[str, str] = {}
    if not ext_dir.exists():
        return result
    for id_dir in ext_dir.iterdir():
        if not id_dir.is_dir():
            continue
        best = _best_version_dir(id_dir)
        if best and _is_webstore_extension(best):
            name = _extension_name(best)
            result[id_dir.name] = name if name != id_dir.name else ""
    return result


def _read_webstore_ids(sync_dir: Path) -> set[str]:
    manifest_path = sync_dir / "webstore_extensions.json"
    if not manifest_path.exists():
        return set()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return set()
    if isinstance(data, dict):
        return {ext_id for ext_id in data if isinstance(ext_id, str)}
    if isinstance(data, list):
        return {ext_id for ext_id in data if isinstance(ext_id, str)}
    return set()


def _extension_dir_ids(base: Path) -> set[str]:
    if not base.exists():
        return set()
    return {entry.name for entry in base.iterdir() if entry.is_dir()}


def _with_alias_ids(ext_ids: set[str], aliases: dict[str, str] | None) -> set[str]:
    if not aliases:
        return ext_ids
    expanded = set(ext_ids)
    for alias_id, canonical_id in aliases.items():
        if canonical_id in expanded:
            expanded.add(alias_id)
        if alias_id in expanded:
            expanded.add(canonical_id)
    return expanded


def extension_install_ids(
    sync_dir: Path,
    profile_dir: Path | None = None,
    aliases: dict[str, str] | None = None,
) -> set[str]:
    ext_ids = _read_webstore_ids(sync_dir)
    ext_ids.update(_extension_dir_ids(sync_dir / "Extensions"))
    if profile_dir is not None:
        ext_ids.update(_extension_dir_ids(profile_dir / "Extensions"))
    return _with_alias_ids(ext_ids, aliases)


def indexeddb_extension_id(name: str) -> str:
    if not name.startswith("chrome-extension_"):
        return ""
    return name.removeprefix("chrome-extension_").split("_")[0]


def _extension_data_ids(sync_dir: Path) -> set[str]:
    ext_ids: set[str] = set()
    for subdir in ("Local Extension Settings", "Sync Extension Settings"):
        ext_ids.update(_extension_dir_ids(sync_dir / subdir))
    indexed_db = sync_dir / "IndexedDB"
    if indexed_db.exists():
        for entry in indexed_db.iterdir():
            if entry.is_dir() and (ext_id := indexeddb_extension_id(entry.name)):
                ext_ids.add(ext_id)
    prefs_path = sync_dir / "preferences.json"
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return ext_ids
        extensions = prefs.get("extensions", {})
        if isinstance(extensions, dict):
            pinned = extensions.get("pinned_extensions")
            if isinstance(pinned, list):
                ext_ids.update(ext_id for ext_id in pinned if isinstance(ext_id, str))
            settings = extensions.get("settings")
            if isinstance(settings, dict):
                ext_ids.update(ext_id for ext_id in settings if isinstance(ext_id, str))
    return ext_ids


def _profile_extension_data_ids(profile_dir: Path) -> set[str]:
    ext_ids: set[str] = set()
    for subdir in (
        "Local Extension Settings",
        "Sync Extension Settings",
        "Managed Extension Settings",
    ):
        ext_ids.update(_extension_dir_ids(profile_dir / subdir))
    indexed_db = profile_dir / "IndexedDB"
    if indexed_db.exists():
        for entry in indexed_db.iterdir():
            if entry.is_dir() and (ext_id := indexeddb_extension_id(entry.name)):
                ext_ids.add(ext_id)
    return ext_ids


def remove_extension_data(
    sync_dir: Path,
    ext_ids: set[str],
    *,
    remove_install_records: bool = False,
) -> None:
    if not ext_ids:
        return
    manifest_path = sync_dir / "webstore_extensions.json"
    if remove_install_records and manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for ext_id in ext_ids:
                    data.pop(ext_id, None)
            elif isinstance(data, list):
                data = [ext_id for ext_id in data if ext_id not in ext_ids]
            else:
                return
            manifest_path.write_text(json.dumps(data), encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass

    subdirs = ["Local Extension Settings", "Sync Extension Settings"]
    if remove_install_records:
        subdirs.append("Extensions")
    for ext_id in ext_ids:
        for subdir in subdirs:
            target = sync_dir / subdir / ext_id
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

    indexed_db = sync_dir / "IndexedDB"
    if indexed_db.exists():
        prefixes = tuple(f"chrome-extension_{ext_id}_" for ext_id in ext_ids)
        for entry in list(indexed_db.iterdir()):
            if entry.name.startswith(prefixes):
                shutil.rmtree(entry, ignore_errors=True)

    prefs_path = sync_dir / "preferences.json"
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            extensions = prefs.get("extensions", {})
            if not isinstance(extensions, dict):
                return
            changed = False
            pinned = extensions.get("pinned_extensions")
            if isinstance(pinned, list):
                kept = [ext_id for ext_id in pinned if ext_id not in ext_ids]
                if kept != pinned:
                    extensions["pinned_extensions"] = kept
                    changed = True
            settings = extensions.get("settings")
            if isinstance(settings, dict):
                for ext_id in ext_ids:
                    if settings.pop(ext_id, None) is not None:
                        changed = True
            if changed:
                prefs_path.write_text(json.dumps(prefs), encoding="utf-8")
        except (json.JSONDecodeError, OSError):
            pass


def prune_orphan_extension_data(sync_dir: Path, keep_ext_ids: set[str]) -> set[str]:
    orphan_ids = _extension_data_ids(sync_dir) - keep_ext_ids
    remove_extension_data(sync_dir, orphan_ids)
    return orphan_ids


def _drop_ids(ext_ids: list[str], drop: set[str], log_msg: str, *log_args: object) -> list[str]:
    if not drop:
        return ext_ids
    kept = [e for e in ext_ids if e not in drop]
    skipped = len(ext_ids) - len(kept)
    if skipped:
        _LOG.info(log_msg, skipped, *log_args)
    return kept


def _filter_install_ids(
    ext_ids: list[str],
    browser: BrowserBase,
    ungoogled_only_ext_ids: list[str],
    windows_only_ext_ids: list[str] | None,
) -> list[str]:
    ext_ids = _drop_ids(
        ext_ids,
        set(browser.ext_id_aliases.values()),
        "Skipping %d internally-bundled extension(s) for %s",
        browser.name,
    )
    if not browser.ungoogled:
        ext_ids = _drop_ids(
            ext_ids,
            set(ungoogled_only_ext_ids),
            "Skipping %d ungoogled-only extension(s) for non-ungoogled browser %s",
            browser.name,
        )
    if platform.system() != "Windows" and windows_only_ext_ids:
        ext_ids = _drop_ids(
            ext_ids,
            set(windows_only_ext_ids),
            "Skipping %d windows-only extension(s) on non-Windows platform",
        )
    return ext_ids


def _wipe_stubs(ext_dir: Path | None, log_msg: str) -> None:
    if ext_dir is None or not ext_dir.exists():
        return
    for stub in ext_dir.glob("*.json"):
        stub.unlink(missing_ok=True)
        _LOG.info(log_msg, stub.stem)


def install_external_extensions(
    sync_profile_path: Path,
    browser: BrowserBase,
    *,
    ungoogled_only_ext_ids: list[str],
    windows_only_ext_ids: list[str] | None = None,
) -> None:
    manifest_path = sync_profile_path / "webstore_extensions.json"
    if not manifest_path.exists():
        return

    try:
        ext_ids = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _LOG.warning("Failed to read webstore_extensions.json")
        return

    if not ext_ids:
        return

    ext_ids = _filter_install_ids(
        ext_ids,
        browser,
        ungoogled_only_ext_ids,
        windows_only_ext_ids,
    )

    update_url = browser.web_store_update_url
    system = platform.system()
    reg_key = browser.windows_extensions_registry_key() if system == "Windows" else None
    linux_policy_dir = browser.linux_managed_policy_dir() if system == "Linux" else None
    macos_domain = browser.macos_policy_domain() if system == "Darwin" else None
    ext_dir = browser.external_extensions_dir()

    if reg_key:
        _install_via_registry(ext_ids, reg_key, update_url)
        force_key = browser.windows_force_list_registry_key()
        if force_key:
            _install_via_force_list(ext_ids, force_key)
        _wipe_stubs(ext_dir, "Removed orphaned extension stub (now using registry): %s")
    elif linux_policy_dir:
        if _install_via_linux_policy(ext_ids, linux_policy_dir, browser.name):
            _wipe_stubs(ext_dir, "Removed orphaned extension stub (now using policy): %s")
        elif ext_dir is not None:
            _install_via_stubs(ext_ids, ext_dir, update_url)
    elif macos_domain:
        _install_via_macos_profile(ext_ids, macos_domain, browser.name)
        _wipe_stubs(ext_dir, "Removed orphaned extension stub (now using profile): %s")
    elif ext_dir is not None:
        _install_via_stubs(ext_ids, ext_dir, update_url)
    else:
        _LOG.info(
            "%s: extension auto-install not supported — %d extension(s) need manual install:",
            browser.name,
            len(ext_ids),
        )
        for ext_id in ext_ids:
            _LOG.info("  https://chromewebstore.google.com/detail/%s", ext_id)


def _install_via_registry(ext_ids: list[str], reg_subkey: str, update_url: str) -> None:
    import winreg

    ext_id_set = set(ext_ids)
    existing: set[str] = set()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_subkey) as base:
            existing = set(_winreg_enum_subkeys(base))
    except FileNotFoundError:
        pass

    for stale_id in existing:
        if stale_id not in ext_id_set:
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, rf"{reg_subkey}\{stale_id}")
                _LOG.info("Removed stale registry extension: %s", stale_id)
            except OSError:
                _LOG.warning("Failed to remove stale registry extension: %s", stale_id)

    for ext_id in ext_ids:
        if ext_id in existing:
            continue
        key_path = rf"{reg_subkey}\{ext_id}"
        try:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                winreg.SetValueEx(key, "update_url", 0, winreg.REG_SZ, update_url)
            _LOG.info("Registered Web Store extension via registry: %s", ext_id)
        except OSError:
            _LOG.warning("Failed to register extension in registry: %s", ext_id)


def _install_via_force_list(ext_ids: list[str], force_key: str) -> None:
    import winreg

    target_set = set(ext_ids)
    existing_map: dict[str, str] = {}  # ext_id -> value_name
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, force_key) as key:
            for name in _winreg_enum_values(key):
                try:
                    val, _ = winreg.QueryValueEx(key, name)
                    ext_id = val.split(";")[0]
                    existing_map[ext_id] = name
                except OSError:
                    pass
    except FileNotFoundError:
        pass

    try:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, force_key) as key:
            for ext_id, val_name in existing_map.items():
                if ext_id not in target_set:
                    winreg.DeleteValue(key, val_name)
                    _LOG.info("Removed stale force-list entry: %s", ext_id)

            used_names = {v for k, v in existing_map.items() if k in target_set}
            next_i = 1
            for ext_id in ext_ids:
                if ext_id in existing_map:
                    continue
                while str(next_i) in used_names:
                    next_i += 1
                winreg.SetValueEx(key, str(next_i), 0, winreg.REG_SZ, ext_id)
                used_names.add(str(next_i))
                _LOG.info("Added force-list entry: %s", ext_id)
                next_i += 1
    except OSError:
        _LOG.warning("Failed to update ExtensionInstallForcelist")


def _linux_policy_filename(browser_name: str) -> str:
    return f"{browser_name.lower().replace(' ', '-')}-syncer.json"


def _install_via_linux_policy(ext_ids: list[str], policy_dir: Path, browser_name: str) -> bool:
    if shutil.which("pkexec") is None:
        _LOG.warning("pkexec not found — cannot install force-list policy on Linux")
        return False

    target = policy_dir / _linux_policy_filename(browser_name)
    payload = json.dumps(
        {"ExtensionInstallForcelist": sorted(ext_ids)},
        indent=2,
    )

    try:
        if target.read_text(encoding="utf-8") == payload:
            return True
    except (OSError, ValueError):
        pass

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as f:
        f.write(payload)
        tmp_path = f.name

    try:
        cmd = [
            "pkexec",
            "sh",
            "-c",
            f"mkdir -p {shlex.quote(str(policy_dir))} && "
            f"install -m 0644 {shlex.quote(tmp_path)} {shlex.quote(str(target))}",
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            _LOG.info(
                "Installed %s force-list policy at %s: %d extensions",
                browser_name,
                target,
                len(ext_ids),
            )
            return True
        else:
            _LOG.warning(
                "Failed to install %s policy via pkexec (rc=%d): %s",
                browser_name,
                result.returncode,
                result.stderr.strip(),
            )
            return False
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _macos_profile_identifier(browser_name: str) -> str:
    slug = browser_name.lower().replace(" ", "-")
    return f"com.chromium-profile-syncer.{slug}.extension-forcelist"


def _macos_stale_profile_identifiers(browser_name: str) -> list[str]:
    slug = browser_name.lower().replace(" ", "-")
    return [
        f"com.chromium-profile-syncer.{slug}.extensions",
        f"com.chromium-profile-syncer.{slug}.external-extensions",
    ]


def _remove_stale_macos_profiles(browser_name: str) -> None:
    for identifier in _macos_stale_profile_identifiers(browser_name):
        try:
            result = subprocess.run(
                ["profiles", "remove", "-identifier", identifier],
                capture_output=True,
                text=True,
            )
        except OSError:
            continue
        if result.returncode == 0:
            _LOG.info("Removed stale macOS profile: %s", identifier)


def _macos_profile_path(browser_name: str) -> Path:
    from src import config as _config

    return _config.CONFIG_DIR / "profiles" / f"{browser_name.lower()}-extensions.mobileconfig"


def _macos_extension_install_settings(ext_ids: list[str]) -> dict:
    return {"ExtensionInstallForcelist": sorted(set(ext_ids))}


def _macos_profile_payload(ext_ids: list[str], domain: str, browser_name: str) -> dict:
    identifier = _macos_profile_identifier(browser_name)
    return {
        "PayloadContent": [
            {
                "PayloadContent": {
                    domain: {
                        "Forced": [
                            {"mcx_preference_settings": _macos_extension_install_settings(ext_ids)}
                        ]
                    }
                },
                "PayloadDisplayName": f"{browser_name} extension policy",
                "PayloadIdentifier": f"{identifier}.payload",
                "PayloadType": "com.apple.ManagedClient.preferences",
                "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{identifier}.payload")),
                "PayloadVersion": 1,
            }
        ],
        "PayloadDescription": f"Forces {browser_name} to install synced Web Store extensions.",
        "PayloadDisplayName": f"{browser_name} Synced Extensions",
        "PayloadIdentifier": identifier,
        "PayloadOrganization": "Chromium Profile Syncer",
        "PayloadRemovalDisallowed": False,
        "PayloadType": "Configuration",
        "PayloadUUID": str(uuid.uuid5(uuid.NAMESPACE_URL, identifier)),
        "PayloadVersion": 1,
    }


def _iter_dicts(value: object):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _macos_profile_has_policy(data: object, ext_ids: list[str], domain: str) -> bool:
    target = _macos_extension_install_settings(ext_ids)
    for item in _iter_dicts(data):
        managed = item.get("PayloadContentManagedPreferences")
        if not isinstance(managed, dict):
            continue
        domain_data = managed.get(domain)
        if not isinstance(domain_data, dict):
            continue
        forced = domain_data.get("Forced")
        if not isinstance(forced, list):
            continue
        for entry in forced:
            settings = entry.get("mcx_preference_settings") if isinstance(entry, dict) else None
            if isinstance(settings, dict) and all(settings.get(k) == v for k, v in target.items()):
                return True
    return False


def _macos_matching_profiles(data: object, identifier: str):
    if isinstance(data, dict) and data.get("ProfileIdentifier") == identifier:
        yield data
        return
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict) and item.get("ProfileIdentifier") == identifier:
                        yield item


def _macos_profile_installed(ext_ids: list[str], domain: str, browser_name: str) -> bool:
    identifier = _macos_profile_identifier(browser_name)
    try:
        result = subprocess.run(
            [
                "profiles",
                "show",
                "-type",
                "configuration",
                "-identifier",
                identifier,
                "-output",
                "stdout-xml",
            ],
            capture_output=True,
        )
    except OSError:
        return False
    if result.returncode != 0 or not result.stdout:
        return False
    try:
        data = plistlib.loads(result.stdout)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return False
    return any(
        _macos_profile_has_policy(profile, ext_ids, domain)
        for profile in _macos_matching_profiles(data, identifier)
    )


def _install_via_macos_profile(ext_ids: list[str], domain: str, browser_name: str) -> None:
    _remove_stale_macos_profiles(browser_name)
    if _macos_profile_installed(ext_ids, domain, browser_name):
        return

    profile_path = _macos_profile_path(browser_name)
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    payload = plistlib.dumps(
        _macos_profile_payload(ext_ids, domain, browser_name),
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    )
    profile_path.write_bytes(payload)

    try:
        subprocess.run(["open", str(profile_path)], capture_output=True)
        _LOG.warning(
            "Opened %s extension policy profile for installation: %s",
            browser_name,
            profile_path,
        )
    except OSError as e:
        _LOG.warning("Failed to open %s extension policy profile: %s", browser_name, e)


def _install_via_stubs(ext_ids: list[str], ext_dir: Path, update_url: str) -> None:
    ext_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({"external_update_url": update_url})
    ext_id_set = set(ext_ids)

    for stub in ext_dir.glob("*.json"):
        if stub.stem not in ext_id_set:
            stub.unlink(missing_ok=True)
            _LOG.info("Removed stale extension stub: %s", stub.stem)

    for ext_id in ext_ids:
        stub = ext_dir / f"{ext_id}.json"
        if not stub.exists():
            stub.write_text(payload, encoding="utf-8")
            _LOG.info("Registered Web Store extension via stub: %s", ext_id)
