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

    @staticmethod
    def get_profile_name(profile_path: Path) -> str:
        """Read display name from Preferences JSON, fallback to directory name."""
        import json

        try:
            prefs = json.loads((profile_path / "Preferences").read_text(encoding="utf-8"))
            name = prefs.get("profile", {}).get("name", "")
            if name:
                return name
        except (OSError, json.JSONDecodeError, KeyError):
            pass
        return profile_path.name

    def is_running(self) -> bool:
        names_lower = {n.lower() for n in self.process_names}
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info["name"] and proc.info["name"].lower() in names_lower:
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return False
