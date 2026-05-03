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
    def ext_id_aliases(self) -> dict[str, str]:
        return {"blockjmkbacgjkknlgpkjjiijinjdanf": "cjpalhdlnbpafiamejdnhcphjbkeiagm"}

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
        return "chrome.exe"

    @property
    def linux_binary_names(self) -> list[str]:
        return ["helium"]

    @property
    def macos_app_bundle(self) -> str | None:
        return "Helium"

    def windows_extensions_registry_key(self) -> str | None:
        return r"SOFTWARE\imput\Helium\Extensions"

    def windows_force_list_registry_key(self) -> str | None:
        return r"SOFTWARE\Policies\Helium\ExtensionInstallForcelist"

    def linux_managed_policy_dir(self) -> Path | None:
        return Path("/etc/chromium/policies/managed")

