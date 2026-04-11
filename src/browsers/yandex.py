from __future__ import annotations

import os
from pathlib import Path

from .base import BrowserBase


class Yandex(BrowserBase):
    @property
    def name(self) -> str:
        return "Yandex"

    @property
    def process_names(self) -> list[str]:
        return ["yandex", "yandex browser", "yandex.exe"]

    def _windows_path(self) -> Path:
        return Path(os.environ.get("LOCALAPPDATA", "")) / "Yandex" / "YandexBrowser" / "User Data"

    def _macos_path(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "Yandex" / "YandexBrowser"

    def _linux_path(self) -> Path:
        return Path.home() / ".config" / "yandex-browser"
