from __future__ import annotations

import logging
import weakref

from PySide6.QtWidgets import QSystemTrayIcon

_ICON: weakref.ReferenceType | None = None
_LOG = logging.getLogger(__name__)


def set_notification_icon(icon: QSystemTrayIcon) -> None:
    global _ICON
    _ICON = weakref.ref(icon)


def notify(title: str, message: str, duration_ms: int = 5000) -> None:
    _LOG.info("%s: %s", title, message)
    icon = _ICON() if _ICON is not None else None
    if icon is not None and QSystemTrayIcon.supportsMessages():
        icon.showMessage(title, message, QSystemTrayIcon.MessageIcon.Information, duration_ms)
