from __future__ import annotations

import json
import logging
import platform
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
                ext_name, ext_id, profile_ver, sync_ver,
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
                ext_name, ext_id, sync_ver, profile_ver,
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
    if not profile_ext_dir.exists():
        return
    manifest_path = sync_dir / "webstore_extensions.json"
    existing: dict[str, str] = {}
    if manifest_path.exists():
        try:
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing = data if isinstance(data, dict) else {e: "" for e in data}
        except (json.JSONDecodeError, OSError):
            pass
    webstore_map: dict[str, str] = {}
    for id_dir in profile_ext_dir.iterdir():
        if not id_dir.is_dir():
            continue
        ext_id = id_dir.name
        canonical = (aliases or {}).get(ext_id, ext_id)
        best = _best_version_dir(id_dir)
        if best and _is_webstore_extension(best):
            name = _extension_name(best)
            webstore_map[canonical] = name if name != ext_id else existing.get(canonical, ext_id)
        elif canonical != ext_id:
            name = _extension_name(best) if best else ""
            if not name or name == ext_id:
                name = existing.get(canonical, "")
            webstore_map[canonical] = name
    if not webstore_map and existing:
        _LOG.info(
            "Profile has 0 web-store extensions; preserving %d existing manifest entries",
            len(existing),
        )
        return
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


def _drop_ids(
    ext_ids: list[str], drop: set[str], log_msg: str, *log_args: object
) -> list[str]:
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
        ext_ids, set(browser.ext_id_aliases.values()),
        "Skipping %d internally-bundled extension(s) for %s", browser.name,
    )
    if not browser.ungoogled:
        ext_ids = _drop_ids(
            ext_ids, set(ungoogled_only_ext_ids),
            "Skipping %d ungoogled-only extension(s) for non-ungoogled browser %s",
            browser.name,
        )
    if platform.system() != "Windows" and windows_only_ext_ids:
        ext_ids = _drop_ids(
            ext_ids, set(windows_only_ext_ids),
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
        ext_ids, browser, ungoogled_only_ext_ids, windows_only_ext_ids,
    )

    update_url = browser.web_store_update_url
    system = platform.system()
    reg_key = browser.windows_extensions_registry_key() if system == "Windows" else None
    linux_policy_dir = browser.linux_managed_policy_dir() if system == "Linux" else None
    ext_dir = browser.external_extensions_dir()

    if reg_key:
        _install_via_registry(ext_ids, reg_key, update_url)
        force_key = browser.windows_force_list_registry_key()
        if force_key:
            _install_via_force_list(ext_ids, force_key)
        _wipe_stubs(ext_dir, "Removed orphaned extension stub (now using registry): %s")
    elif linux_policy_dir:
        _install_via_linux_policy(ext_ids, linux_policy_dir, browser.name)
        _wipe_stubs(ext_dir, "Removed orphaned extension stub (now using policy): %s")
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


def _install_via_linux_policy(
    ext_ids: list[str], policy_dir: Path, browser_name: str
) -> None:
    import shlex
    import shutil
    import subprocess
    import tempfile

    if shutil.which("pkexec") is None:
        _LOG.warning("pkexec not found — cannot install force-list policy on Linux")
        return

    target = policy_dir / _linux_policy_filename(browser_name)
    payload = json.dumps(
        {"ExtensionInstallForcelist": sorted(ext_ids)},
        indent=2,
    )

    try:
        if target.read_text(encoding="utf-8") == payload:
            return
    except (OSError, ValueError):
        pass

    with tempfile.NamedTemporaryFile(
        "w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
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
                browser_name, target, len(ext_ids),
            )
        else:
            _LOG.warning(
                "Failed to install %s policy via pkexec (rc=%d): %s",
                browser_name, result.returncode, result.stderr.strip(),
            )
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def _clean_linux_policy(policy_dir: Path, browser_name: str) -> None:
    import shutil
    import subprocess

    target = policy_dir / _linux_policy_filename(browser_name)
    if not target.exists():
        return
    if shutil.which("pkexec") is None:
        _LOG.warning("pkexec not found — cannot remove policy file %s", target)
        return

    result = subprocess.run(
        ["pkexec", "rm", "-f", str(target)], capture_output=True, text=True
    )
    if result.returncode == 0:
        _LOG.info("Removed %s force-list policy: %s", browser_name, target)
    else:
        _LOG.warning(
            "Failed to remove policy file %s (rc=%d): %s",
            target, result.returncode, result.stderr.strip(),
        )


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


def clean_external_extensions(browsers: list[BrowserBase]) -> None:
    system = platform.system()
    on_windows = system == "Windows"
    on_linux = system == "Linux"
    for browser in browsers:
        reg_key = browser.windows_extensions_registry_key() if on_windows else None
        linux_policy_dir = browser.linux_managed_policy_dir() if on_linux else None
        if reg_key:
            _wipe_registry_extensions(reg_key)
            force_key = browser.windows_force_list_registry_key()
            if force_key:
                _wipe_registry_key(force_key)
        elif linux_policy_dir:
            _clean_linux_policy(linux_policy_dir, browser.name)
        else:
            _wipe_stubs(browser.external_extensions_dir(), "Removed extension stub: %s")


def _wipe_registry_extensions(reg_subkey: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, reg_subkey) as base:
            ext_ids = _winreg_enum_subkeys(base)
    except FileNotFoundError:
        return

    for ext_id in ext_ids:
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, reg_subkey + "\\" + ext_id)
            _LOG.info("Removed registry entry: %s", ext_id)
        except OSError:
            _LOG.warning("Failed to remove registry entry: %s", ext_id)


def _wipe_registry_key(reg_subkey: str) -> None:
    import winreg

    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, reg_subkey, access=winreg.KEY_ALL_ACCESS
        ) as key:
            for name in _winreg_enum_values(key):
                winreg.DeleteValue(key, name)
        _LOG.info("Cleared registry key: %s", reg_subkey)
    except FileNotFoundError:
        pass
    except OSError:
        _LOG.warning("Failed to clear registry key: %s", reg_subkey)
