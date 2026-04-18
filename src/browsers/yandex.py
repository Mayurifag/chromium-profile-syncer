from __future__ import annotations

from pathlib import Path

from .base import BrowserBase


class Yandex(BrowserBase):
    @property
    def name(self) -> str:
        return "Yandex"

    @property
    def unix_process_names(self) -> list[str]:
        return ["yandex browser", "yandex-browser"]

    @property
    def windows_exe_substr(self) -> str:
        return "\\yandex\\yandexbrowser\\application"

    def _windows_path(self) -> Path:
        return Path(self._localappdata()) / "Yandex" / "YandexBrowser" / "User Data"

    def _macos_path(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "Yandex" / "YandexBrowser"

    def _linux_path(self) -> Path:
        return Path.home() / ".config" / "yandex-browser"

    @property
    def windows_executable_name(self) -> str | None:
        return "browser.exe"
