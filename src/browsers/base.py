from __future__ import annotations

import platform
import re
from abc import ABC, abstractmethod
from pathlib import Path

import psutil


class BrowserBase(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def process_names(self) -> list[str]: ...

    @abstractmethod
    def _windows_path(self) -> Path: ...

    @abstractmethod
    def _macos_path(self) -> Path: ...

    @abstractmethod
    def _linux_path(self) -> Path: ...

    def profile_root(self) -> Path | None:
        system = platform.system()
        if system == "Windows":
            return self._windows_path()
        elif system == "Darwin":
            return self._macos_path()
        else:
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
        """Return the External Extensions directory for this browser, or None."""
        root = self.profile_root()
        return (root / "External Extensions") if root else None

    def windows_extensions_registry_key(self) -> str | None:
        """Return the HKCU registry subkey for external extension registration on Windows.

        Chrome on Windows ignores the file-based External Extensions directory;
        the Registry is the only supported mechanism. Other browsers may fall back
        to the file-based approach, so return None by default.
        """
        return None

    def windows_force_list_registry_key(self) -> str | None:
        """Return the HKCU registry subkey for ExtensionInstallForcelist policy.

        Force-listed extensions install and enable automatically with no user prompt.
        Returns None by default; browsers that support this policy override it.
        """
        return None

    def get_profile_name(self, profile_path: Path) -> str:
        """Read display name from Local State or Preferences, fallback to directory name."""
        import json

        profile_dir_name = profile_path.name

        # Try reading from Local State first (has email and better names)
        try:
            root = self.profile_root()
            if root:
                local_state_path = root / "Local State"
                if local_state_path.exists():
                    local_state = json.loads(local_state_path.read_text(encoding="utf-8"))
                    info_cache = local_state.get("profile", {}).get("info_cache", {})
                    profile_info = info_cache.get(profile_dir_name, {})

                    # Priority: custom name (if not default) > email > gaia_name
                    is_default = profile_info.get("is_using_default_name", True)
                    name = profile_info.get("name", "").strip()
                    if name and not is_default:
                        return name

                    user_name = profile_info.get("user_name", "").strip()
                    if user_name:
                        return user_name

                    gaia_name = profile_info.get("gaia_name", "").strip()
                    if gaia_name:
                        return gaia_name
        except (OSError, json.JSONDecodeError, KeyError):
            pass

        # Fallback to Preferences file
        try:
            prefs = json.loads((profile_path / "Preferences").read_text(encoding="utf-8"))
            name = prefs.get("profile", {}).get("name", "").strip()
            if name:
                return name
        except (OSError, json.JSONDecodeError, KeyError):
            pass

        return profile_dir_name

    def is_running(self) -> bool:
        names_lower = {n.lower() for n in self.process_names}
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() in names_lower:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
