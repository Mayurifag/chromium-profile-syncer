from .base import BrowserBase
from .chrome import Chrome
from .helium import Helium
from .thorium import Thorium
from .yandex import Yandex

ALL_BROWSERS: list[BrowserBase] = [Thorium(), Helium(), Chrome(), Yandex()]

__all__ = ["ALL_BROWSERS", "BrowserBase", "Chrome", "Helium", "Thorium", "Yandex"]
