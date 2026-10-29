from __future__ import annotations

import json
import logging
import platform
import re
import subprocess
from pathlib import Path

from src import config
from src.browsers.base import BrowserBase
from src.sync.extensions import read_library

_LOG = logging.getLogger(__name__)
_EXTENSION_ID = re.compile(r"[a-p]{32}\Z")


def _registry_values(key):
    import winreg

    index = 0
    while True:
        try:
            yield winreg.EnumValue(key, index)
        except OSError:
            return
        index += 1


def release_forced_extensions(browser: BrowserBase, known_ids: set[str]) -> None:
    """Remove only legacy syncer entries, never whole shared policy keys."""
    system = platform.system()
    if system == "Windows":
        import winreg

        force_key = browser.windows_force_list_registry_key()
        if force_key:
            try:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    force_key,
                    0,
                    winreg.KEY_READ | winreg.KEY_SET_VALUE,
                ) as key:
                    for name, value, _kind in list(_registry_values(key)):
                        # The old syncer wrote bare IDs. URL-bearing policies may be user-owned.
                        if isinstance(value, str) and value in known_ids:
                            winreg.DeleteValue(key, name)
                            _LOG.info("Released forced extension: %s", value)
            except FileNotFoundError:
                pass
    elif system == "Linux" and (policy_dir := browser.linux_managed_policy_dir()):
        path = policy_dir / f"{browser.name.lower().replace(' ', '-')}-syncer.json"
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if set(data) != {"ExtensionInstallForcelist"}:
                raise ValueError(f"Unexpected legacy policy content; remove manually: {path}")
            result = subprocess.run(
                ["pkexec", "rm", "--", str(path)],
                capture_output=True,
                text=True,
                timeout=120,
            )
            if result.returncode:
                raise OSError(f"Cannot release extension policy {path}: {result.stderr.strip()}")
    elif system == "Darwin" and browser.macos_policy_domain():
        slug = browser.name.lower().replace(" ", "-")
        result = subprocess.run(
            ["profiles", "list", "-type", "configuration"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode:
            raise OSError("Cannot inspect macOS profiles; remove the syncer profile in Settings")
        for suffix in ("extension-forcelist", "extensions", "external-extensions"):
            identifier = f"com.chromium-profile-syncer.{slug}.{suffix}"
            if identifier not in result.stdout:
                continue
            removed = subprocess.run(
                ["profiles", "remove", "-identifier", identifier],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if removed.returncode:
                raise OSError(f"Remove {identifier} in macOS Settings to allow extension control")


def install_external_extensions(
    sync_profile_path: Path,
    browser: BrowserBase,
    *,
    ungoogled_only_ext_ids: list[str],
    windows_only_ext_ids: list[str] | None = None,
) -> None:
    library = read_library(sync_profile_path)
    if not library:
        return
    known_ids = {ext_id for ext_id in library if _EXTENSION_ID.fullmatch(ext_id)}
    if browser.name not in config.get_released_extension_policies():
        release_forced_extensions(browser, known_ids)
        config.mark_extension_policies_released(browser.name)
    excluded = set(browser.ext_id_aliases.values())
    if not browser.ungoogled:
        excluded.update(ungoogled_only_ext_ids)
    if platform.system() != "Windows":
        excluded.update(windows_only_ext_ids or [])
    active = {ext_id for ext_id, entry in library.items() if not entry["archived"]}
    active = active & known_ids - excluded
    reg_key = browser.windows_extensions_registry_key() if platform.system() == "Windows" else None
    if reg_key:
        _register_windows(browser, reg_key, active, known_ids)
    elif (ext_dir := browser.external_extensions_dir()) is not None:
        _register_stubs(browser, ext_dir, active, known_ids)
    elif active:
        _LOG.warning(
            "%s: install synced extensions manually from the Extensions dialog", browser.name
        )


def _register_windows(
    browser: BrowserBase, base: str, active: set[str], known_ids: set[str]
) -> None:
    import winreg

    for ext_id in sorted(known_ids):
        path = rf"{base}\{ext_id}"
        if ext_id in active:
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, path) as key:
                try:
                    winreg.QueryValueEx(key, "update_url")
                except FileNotFoundError:
                    winreg.SetValueEx(
                        key, "update_url", 0, winreg.REG_SZ, browser.web_store_update_url
                    )
        else:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as key:
                    url, _kind = winreg.QueryValueEx(key, "update_url")
                    values = list(_registry_values(key))
                if url == browser.web_store_update_url and len(values) == 1:
                    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, path)
            except FileNotFoundError:
                pass


def _register_stubs(
    browser: BrowserBase, directory: Path, active: set[str], known_ids: set[str]
) -> None:
    payload = {"external_update_url": browser.web_store_update_url}
    for ext_id in sorted(known_ids):
        stub = directory / f"{ext_id}.json"
        if ext_id in active:
            if not stub.exists():
                directory.mkdir(parents=True, exist_ok=True)
                stub.write_text(json.dumps(payload), encoding="utf-8")
        elif stub.exists() and json.loads(stub.read_text(encoding="utf-8")) == payload:
            stub.unlink()
