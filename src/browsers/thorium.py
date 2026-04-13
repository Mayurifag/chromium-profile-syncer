from __future__ import annotations

import os
from pathlib import Path

from .base import BrowserBase


class Thorium(BrowserBase):
    @property
    def name(self) -> str:
        return "Thorium"

    @property
    def process_names(self) -> list[str]:
        return ["thorium", "thorium-browser", "thorium.exe"]

    def _windows_path(self) -> Path:
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Thorium" / "User Data"

    def _macos_path(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "Thorium"

    def _linux_path(self) -> Path:
        return Path.home() / ".config" / "thorium"

    def windows_extensions_registry_key(self) -> str | None:
        # Thorium reads from Chrome's registry path, not its own
        return r"SOFTWARE\Google\Chrome\Extensions"

    def windows_force_list_registry_key(self) -> str | None:
        return r"SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"
