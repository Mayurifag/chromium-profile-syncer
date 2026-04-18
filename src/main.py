from __future__ import annotations

import argparse
import logging
import pathlib
import sys
import tempfile

from PySide6.QtWidgets import QApplication

from src import autostart, config, single_instance
from src.dracula import DRACULA_STYLESHEET
from src.sync_engine import SyncEngine
from src.tray import TrayApp, make_app_icon


def main() -> None:
    parser = argparse.ArgumentParser(description="Chromium Profile Syncer")
    parser.add_argument(
        "--tray",
        action="store_true",
        help="Start in tray-only mode (for autostart)",
    )
    parser.add_argument(
        "--remove-profile",
        metavar="BROWSER",
        help="Delete browser profile from disk, remove from config, delete archive, then exit",
    )
    parser.add_argument(
        "--sync",
        action="store_true",
        help="Run sync (respects config; combine with --browser/--profile/--direction), then exit",
    )
    parser.add_argument(
        "--restore-from",
        metavar="TAR",
        help="Restore profile data from a tar archive, then exit",
    )
    parser.add_argument(
        "--browser",
        metavar="BROWSER",
        help="Target browser (for --sync or --restore-from)",
    )
    parser.add_argument(
        "--profile",
        metavar="PROFILE",
        help="Target profile name (for --sync)",
    )
    parser.add_argument(
        "--direction",
        metavar="DIRECTION",
        choices=["push", "pull", "both"],
        default=None,
        help="Sync direction for --sync: push, pull, or both",
    )
    args = parser.parse_args()

    if args.remove_profile:
        import shutil

        from src.browsers import get_browser
        browser = get_browser(args.remove_profile)
        if browser is None:
            print(f"Unknown browser: {args.remove_profile}")
            sys.exit(1)
        enabled = config.get_enabled_profiles().get(browser.name, [])
        root = browser.profile_root()
        for profile_name in enabled:
            profile_dir = root / profile_name if root else None
            if profile_dir and profile_dir.is_dir():
                shutil.rmtree(profile_dir)
                print(f"Deleted {profile_dir}")
        config.remove_browser_profile(browser.name)
        sync_folder = config.get_sync_folder()
        if sync_folder:
            archive = sync_folder / "current.tar"
            if archive.is_file():
                archive.unlink()
                print(f"Deleted {archive}")
        print(f"Profile '{browser.name}' removed. Start the app to upload fresh.")
        sys.exit(0)

    if args.sync:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
        sync_folder = config.get_sync_folder()
        if sync_folder is None:
            print("ERROR: sync_folder not configured — run the app first")
            sys.exit(1)
        engine = SyncEngine(sync_folder)
        result = engine.sync_all(
            only_browser=args.browser,
            only_profile=args.profile,
            force_direction=args.direction,
            on_progress=lambda m: print(f"  {m}"),
        )
        skipped = result.get("skipped_running", [])
        if skipped:
            print(f"Skipped (running): {', '.join(skipped)}")
        sys.exit(0)

    if args.restore_from:
        from src.browsers import ALL_BROWSERS, get_browser

        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

        tar_path = pathlib.Path(args.restore_from).expanduser().resolve()
        if not tar_path.exists():
            print(f"Archive not found: {tar_path}")
            sys.exit(1)

        if args.browser:
            browser = get_browser(args.browser)
            if browser is None:
                print(f"Unknown browser: {args.browser}")
                sys.exit(1)
            target_browsers = [browser]
        else:
            target_browsers = list(ALL_BROWSERS)

        engine = SyncEngine(tar_path.parent)
        engine.restore_from_archive(
            tar_path, target_browsers, on_progress=lambda m: print(f"  {m}"),
        )
        print("Restore complete.")
        sys.exit(0)

    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    single_instance.acquire()

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DRACULA_STYLESHEET)
    app.setWindowIcon(make_app_icon())

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

    if not args.tray:
        QTimer.singleShot(0, tray.open_settings)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
