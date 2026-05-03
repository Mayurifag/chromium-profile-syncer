from __future__ import annotations

from pathlib import Path

from .base import BrowserBase


class Chrome(BrowserBase):
    @property
    def name(self) -> str:
        return "Chrome"

    @property
    def unix_process_names(self) -> list[str]:
        return ["chrome", "google chrome"]

    @property
    def windows_exe_substr(self) -> str:
        return "\\google\\chrome\\application"

    def _windows_path(self) -> Path:
        return Path(self._localappdata()) / "Google" / "Chrome" / "User Data"

    def _macos_path(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "Google" / "Chrome"

    def _linux_path(self) -> Path:
        return Path.home() / ".config" / "google-chrome"

    @property
    def windows_executable_name(self) -> str | None:
        return "chrome.exe"

    @property
    def linux_binary_names(self) -> list[str]:
        return ["google-chrome-stable", "google-chrome", "chromium"]

    @property
    def macos_app_bundle(self) -> str | None:
        return "Google Chrome"

    def windows_extensions_registry_key(self) -> str | None:
        return r"SOFTWARE\Google\Chrome\Extensions"

    def windows_force_list_registry_key(self) -> str | None:
        return r"SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"
