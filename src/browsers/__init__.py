from .base import BrowserBase
from .chrome import Chrome
from .helium import Helium
from .thorium import Thorium
from .yandex import Yandex

ALL_BROWSERS: list[BrowserBase] = [Thorium(), Helium(), Chrome(), Yandex()]


def get_browser(name: str) -> BrowserBase | None:
    return next((b for b in ALL_BROWSERS if b.name.lower() == name.lower()), None)


__all__ = ["ALL_BROWSERS", "BrowserBase", "Chrome", "Helium", "Thorium", "Yandex", "get_browser"]
