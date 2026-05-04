from __future__ import annotations

import json
import os
import platform
import re
import shutil
import sys
from abc import ABC, abstractmethod
from pathlib import Path

import psutil


def scan_running_procs() -> set[str]:
    on_windows = sys.platform == "win32"
    attr = "exe" if on_windows else "name"
    results: set[str] = set()
    for proc in psutil.process_iter([attr]):
        try:
            val = proc.info.get(attr)
            if val:
                results.add(val.lower())
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return results


class BrowserBase(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def unix_process_names(self) -> list[str]: ...

    @property
    @abstractmethod
    def windows_exe_substr(self) -> str: ...

    @property
    def ungoogled(self) -> bool:
        return False

    @property
    def ext_id_aliases(self) -> dict[str, str]:
        """Maps browser-internal extension IDs to their canonical Web Store IDs."""
        return {}

    @property
    def web_store_update_url(self) -> str:
        return "https://clients2.google.com/service/update2/crx"

    @staticmethod
    def _localappdata() -> str:
        return os.environ.get("LOCALAPPDATA", "")

    @abstractmethod
    def _windows_path(self) -> Path: ...

    @abstractmethod
    def _macos_path(self) -> Path: ...

    @abstractmethod
    def _linux_path(self) -> Path: ...

    def profile_root(self) -> Path | None:
        match platform.system():
            case "Windows":
                return self._windows_path()
            case "Darwin":
                return self._macos_path()
            case _:
                return self._linux_path()

    def is_installed(self) -> bool:
        root = self.profile_root()
        return root is not None and root.exists()

    def discover_profiles(self) -> list[Path]:
        root = self.profile_root()
        if root is None or not root.exists():
            return []
        pattern = re.compile(r"^(Default|Profile \d+)$")
        profiles: list[Path] = []
        for entry in sorted(root.iterdir()):
            if entry.is_dir() and pattern.match(entry.name) and (entry / "Preferences").exists():
                profiles.append(entry)
        return profiles

    def external_extensions_dir(self) -> Path | None:
        root = self.profile_root()
        return (root / "External Extensions") if root else None

    def local_state_path(self) -> Path | None:
        root = self.profile_root()
        return (root / "Local State") if root else None

    def windows_extensions_registry_key(self) -> str | None:
        return None

    def windows_force_list_registry_key(self) -> str | None:
        return None

    def linux_managed_policy_dir(self) -> Path | None:
        return None

    def macos_managed_pref_domain(self) -> str | None:
        return None

    @property
    def windows_executable_name(self) -> str | None:
        return None

    def executable(self) -> Path | None:
        if sys.platform != "win32":
            return None
        exe_name = self.windows_executable_name
        if not exe_name:
            return None
        root = self.profile_root()
        if not root:
            return None
        exe = root.parent / "Application" / exe_name
        return exe if exe.exists() else None

    @property
    def linux_binary_names(self) -> list[str]:
        return []

    @property
    def macos_app_bundle(self) -> str | None:
        return None

    def launch_command(self) -> list[str] | None:
        if sys.platform == "win32":
            exe = self.executable()
            return [str(exe)] if exe else None
        if sys.platform == "darwin":
            bundle = self.macos_app_bundle
            return ["open", "-a", bundle, "--args"] if bundle else None
        for cand in self.linux_binary_names:
            path = shutil.which(cand)
            if path:
                return [path]
        return None

    def _name_from_local_state(self, profile_dir_name: str) -> str | None:
        root = self.profile_root()
        if not root:
            return None
        local_state_path = root / "Local State"
        if not local_state_path.exists():
            return None
        try:
            local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
            info = local_state.get("profile", {}).get("info_cache", {}).get(profile_dir_name, {})
            if not info.get("is_using_default_name", True):
                name = info.get("name", "").strip()
                if name:
                    return name
            for key in ("user_name", "gaia_name"):
                val = info.get(key, "").strip()
                if val:
                    return val
        except (OSError, json.JSONDecodeError, KeyError):
            pass
        return None

    def get_profile_name(self, profile_path: Path) -> str:
        name = self._name_from_local_state(profile_path.name)
        if name:
            return name
        try:
            prefs = json.loads((profile_path / "Preferences").read_text(encoding="utf-8"))
            name = prefs.get("profile", {}).get("name", "").strip()
            if name:
                return name
        except (OSError, json.JSONDecodeError, KeyError):
            pass
        return profile_path.name

    def is_running(self, running_procs: set[str] | None = None) -> bool:
        if running_procs is None:
            running_procs = scan_running_procs()
        on_windows = sys.platform == "win32"
        if on_windows:
            return any(self.windows_exe_substr in p for p in running_procs)
        return bool({n.lower() for n in self.unix_process_names} & running_procs)
