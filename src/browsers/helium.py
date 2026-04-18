from __future__ import annotations

from pathlib import Path

from .base import BrowserBase


class Helium(BrowserBase):
    @property
    def name(self) -> str:
        return "Helium"

    @property
    def ungoogled(self) -> bool:
        return True

    @property
    def web_store_update_url(self) -> str:
        return "https://services.helium.imput.net/service/update2/crx"

    @property
    def unix_process_names(self) -> list[str]:
        return ["helium"]

    @property
    def windows_exe_substr(self) -> str:
        return "\\imput\\helium\\application"

    def _windows_path(self) -> Path:
        return Path(self._localappdata()) / "imput" / "Helium" / "User Data"

    def _macos_path(self) -> Path:
        return Path.home() / "Library" / "Application Support" / "net.imput.helium"

    def _linux_path(self) -> Path:
        return Path.home() / ".config" / "net.imput.helium"

    @property
    def windows_executable_name(self) -> str | None:
        return "Helium.exe"

    def windows_extensions_registry_key(self) -> str | None:
        return r"SOFTWARE\Chromium\Extensions"

    def windows_force_list_registry_key(self) -> str | None:
        return r"SOFTWARE\Policies\Chromium\ExtensionInstallForcelist"

