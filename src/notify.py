from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

_TOAST: QFrame | None = None

_STYLE = """
QFrame#toast {
    background-color: #282a36;
    border: 1px solid #44475a;
    border-radius: 8px;
}
QLabel#toast_title { color: #bd93f9; font-weight: bold; }
QLabel#toast_body { color: #f8f8f2; }
"""


def notify(title: str, message: str, duration_ms: int = 1000) -> None:
    global _TOAST
    if _TOAST is not None:
        try:
            _TOAST.close()
        except RuntimeError:
            pass
        _TOAST = None

    toast = QFrame(
        None,
        Qt.WindowType.ToolTip
        | Qt.WindowType.FramelessWindowHint
        | Qt.WindowType.WindowStaysOnTopHint,
    )
    toast.setObjectName("toast")
    toast.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
    toast.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
    toast.setStyleSheet(_STYLE)

    layout = QVBoxLayout(toast)
    layout.setContentsMargins(12, 8, 12, 8)
    layout.setSpacing(2)

    title_lbl = QLabel(title)
    title_lbl.setObjectName("toast_title")
    layout.addWidget(title_lbl)

    body_lbl = QLabel(message)
    body_lbl.setObjectName("toast_body")
    body_lbl.setWordWrap(True)
    body_lbl.setMaximumWidth(380)
    layout.addWidget(body_lbl)

    toast.adjustSize()

    screen = QGuiApplication.primaryScreen()
    if screen is not None:
        avail = screen.availableGeometry()
        toast.move(avail.right() - toast.width() - 16, avail.bottom() - toast.height() - 16)

    toast.show()
    QTimer.singleShot(duration_ms, toast.close)
    _TOAST = toast
