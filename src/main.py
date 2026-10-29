from __future__ import annotations

import argparse
import logging
import sys

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication

from src import autostart, cli, config
from src.dracula import DRACULA_STYLESHEET
from src.single_instance import SingleInstance, send_command
from src.sync_engine import SyncEngine
from src.tray import TrayApp, make_app_icon


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if len(sys.argv) > 1 and sys.argv[1] in cli.COMMANDS:
        sys.exit(cli.run(sys.argv[1:]))
    parser = argparse.ArgumentParser(description="Chromium Profile Syncer")
    parser.add_argument("--tray", action="store_true", help="Start without opening settings")
    parser.add_argument(
        "--quit", action="store_true", help="Ask the running app to finish and quit"
    )
    args = parser.parse_args()
    app = QApplication(sys.argv)
    app.setApplicationName("chromium-profile-syncer")
    if args.quit:
        sys.exit(0 if send_command("quit") else 1)
    instance = SingleInstance(app)
    if not instance.acquire(show_existing=not args.tray):
        sys.exit(0)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DRACULA_STYLESHEET)
    app.setWindowIcon(make_app_icon())
    folder = config.get_sync_folder()
    tray = TrayApp(SyncEngine(folder or config.CONFIG_DIR / "unconfigured-sync"), config)
    tray.show()
    instance.show_requested.connect(tray.open_settings)
    instance.quit_requested.connect(tray.request_quit)
    app.aboutToQuit.connect(tray.wait_for_workers)
    app.aboutToQuit.connect(instance.close)
    autostart.apply(config.get_autostart() and any(config.get_enabled_profiles().values()))
    if not args.tray:
        QTimer.singleShot(0, tray.open_settings)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
