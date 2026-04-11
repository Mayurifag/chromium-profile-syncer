from __future__ import annotations

from pathlib import Path

from .base import BrowserBase


class Chrome(BrowserBase):
    @property
    def name(self) -> str:
        return "Chrome"

    @property
    def process_names(self) -> list[str]:
        return ["google chrome", "chrome", "chrome.exe"]

    def _windows_path(self) -> Path:
        import os

        return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"

    def _macos_path(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"

    def _linux_path(self) -> Path:
        return Path.home() / ".config" / "google-chrome"
