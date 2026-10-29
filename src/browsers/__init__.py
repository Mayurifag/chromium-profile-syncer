from .base import BrowserBase

_CHROME_EXTENSIONS = r"SOFTWARE\Google\Chrome\Extensions"
_CHROME_FORCE_LIST = r"SOFTWARE\Policies\Google\Chrome\ExtensionInstallForcelist"

ALL_BROWSERS = [
    BrowserBase(
        name="Thorium",
        unix_process_names=("thorium", "thorium-browser"),
        windows_exe_substr="\\thorium\\application",
        windows_profile=("Thorium", "User Data"),
        macos_profile="Thorium",
        linux_profile="thorium",
        windows_executable_name="thorium.exe",
        linux_binary_names=("thorium-browser", "thorium"),
        macos_app_bundle="Thorium",
        extensions_registry=_CHROME_EXTENSIONS,
        force_list_registry=_CHROME_FORCE_LIST,
    ),
    BrowserBase(
        name="Helium",
        unix_process_names=("helium",),
        windows_exe_substr="\\imput\\helium\\application",
        windows_profile=("imput", "Helium", "User Data"),
        macos_profile="net.imput.helium",
        linux_profile="net.imput.helium",
        windows_executable_name="chrome.exe",
        linux_binary_names=("helium",),
        macos_app_bundle="Helium",
        ungoogled=True,
        ext_id_aliases={"blockjmkbacgjkknlgpkjjiijinjdanf": "cjpalhdlnbpafiamejdnhcphjbkeiagm"},
        extensions_registry=r"SOFTWARE\imput\Helium\Extensions",
        force_list_registry=r"SOFTWARE\Policies\Helium\ExtensionInstallForcelist",
        managed_policy_dir="/etc/chromium/policies/managed",
        policy_domain="net.imput.helium",
    ),
    BrowserBase(
        name="Chrome",
        unix_process_names=("chrome", "google chrome"),
        windows_exe_substr="\\google\\chrome\\application",
        windows_profile=("Google", "Chrome", "User Data"),
        macos_profile="Google/Chrome",
        linux_profile="google-chrome",
        windows_executable_name="chrome.exe",
        linux_binary_names=("google-chrome-stable", "google-chrome", "chromium"),
        macos_app_bundle="Google Chrome",
        extensions_registry=_CHROME_EXTENSIONS,
        force_list_registry=_CHROME_FORCE_LIST,
    ),
    BrowserBase(
        name="Yandex",
        unix_process_names=("yandex browser", "yandex-browser"),
        windows_exe_substr="\\yandex\\yandexbrowser\\application",
        windows_profile=("Yandex", "YandexBrowser", "User Data"),
        macos_profile="Yandex/YandexBrowser",
        linux_profile="yandex-browser",
        windows_executable_name="browser.exe",
        linux_binary_names=("yandex-browser", "yandex-browser-stable"),
        macos_app_bundle="Yandex",
    ),
]


def get_browser(name: str) -> BrowserBase | None:
    return next(
        (browser for browser in ALL_BROWSERS if browser.name.casefold() == name.casefold()), None
    )


__all__ = ["ALL_BROWSERS", "BrowserBase", "get_browser"]
