from __future__ import annotations

import os
from pathlib import Path

from .base import BrowserBase


class Helium(BrowserBase):
    @property
    def name(self) -> str:
        return "Helium"

    @property
    def process_names(self) -> list[str]:
        return ["helium", "helium.exe"]

    def _windows_path(self) -> Path:
        return Path(os.environ.get("LOCALAPPDATA", "")) / "imput" / "Helium" / "User Data"

    def _macos_path(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "net.imput.helium"

    def _linux_path(self) -> Path:
        return Path.home() / ".config" / "net.imput.helium"
