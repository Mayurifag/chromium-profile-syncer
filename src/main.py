from __future__ import annotations

import logging
import pathlib
import sys
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from src import autostart, config
from src.sync_engine import SyncEngine
from src.tray import TrayApp


def _apply_dark_palette(app: QApplication) -> None:
    """Apply Fusion style with a dark palette on non-Linux platforms."""
    app.setStyle("Fusion")
    palette = QPalette()
    dark = QColor(45, 45, 45)
    mid_dark = QColor(60, 60, 60)
    text = QColor(220, 220, 220)
    highlight = QColor(42, 130, 218)

    palette.setColor(QPalette.ColorRole.Window, dark)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, mid_dark)
    palette.setColor(QPalette.ColorRole.AlternateBase, dark)
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(25, 25, 25))
    palette.setColor(QPalette.ColorRole.ToolTipText, text)
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, dark)
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.BrightText, Qt.GlobalColor.red)
    palette.setColor(QPalette.ColorRole.Link, highlight)
    palette.setColor(QPalette.ColorRole.Highlight, highlight)
    palette.setColor(QPalette.ColorRole.HighlightedText, Qt.GlobalColor.black)
    app.setPalette(palette)


def main() -> None:
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    if sys.platform != "linux":
        _apply_dark_palette(app)

    from PySide6.QtCore import QTimer

    sync_folder = config.get_sync_folder()
    first_run = sync_folder is None
    if sync_folder is not None:
        engine = SyncEngine(sync_folder)
    else:
        engine = SyncEngine(pathlib.Path(tempfile.mkdtemp()))

    tray = TrayApp(engine, config)
    tray.show()
    autostart.apply(config.get_autostart())

    if first_run:
        QTimer.singleShot(0, tray.open_settings)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
