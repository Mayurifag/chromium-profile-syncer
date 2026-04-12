from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import tempfile

from PySide6.QtWidgets import QApplication

from src import autostart, config, single_instance
from src.sync_engine import SyncEngine
from src.theme import DRACULA_STYLESHEET
from src.tray import TrayApp


def main() -> None:
    parser = argparse.ArgumentParser(description="Chromium Profile Syncer")
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Start in tray-only mode (for autostart)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    single_instance.acquire()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DRACULA_STYLESHEET)

    from PySide6.QtCore import QTimer

    sync_folder = config.get_sync_folder()
    if sync_folder is not None:
        engine = SyncEngine(sync_folder)
    else:
        engine = SyncEngine(pathlib.Path(tempfile.mkdtemp()))

    tray = TrayApp(engine, config)
    tray.show()

    single_instance.setup_signal_handler(tray.open_settings)

    has_profiles = any(profiles for profiles in config.get_enabled_profiles().values())
    autostart.apply(has_profiles)

    # Open settings dialog on manual launch (not autostart)
    if not args.tray:
        QTimer.singleShot(0, tray.open_settings)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
