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
        "--restore-from",
        metavar="TAR",
        help="Restore profile data from a tar archive, then exit",
    )
    parser.add_argument(
        "--browser",
        metavar="BROWSER",
        help="Target browser for --restore-from",
    )
    args = parser.parse_args()

    if args.remove_profile:
        import shutil

        from src.browsers import ALL_BROWSERS
        browser_name = args.remove_profile
        browser = next((b for b in ALL_BROWSERS if b.name.lower() == browser_name.lower()), None)
        if browser is None:
            print(f"Unknown browser: {browser_name}")
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

    if args.restore_from:
        import shutil

        from src.browsers import ALL_BROWSERS
        from src.sync import archive as _archive
        from src.sync import extensions as _extensions

        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

        tar_path = pathlib.Path(args.restore_from).expanduser().resolve()
        if not tar_path.exists():
            print(f"Archive not found: {tar_path}")
            sys.exit(1)

        browser_name = args.browser
        if browser_name:
            target_browsers = [b for b in ALL_BROWSERS if b.name.lower() == browser_name.lower()]
            if not target_browsers:
                print(f"Unknown browser: {browser_name}")
                sys.exit(1)
        else:
            target_browsers = list(ALL_BROWSERS)

        work_dir = pathlib.Path(tempfile.mkdtemp(prefix="cps-restore-"))
        try:
            print(f"Unpacking {tar_path}...")
            _archive.unpack_archive(tar_path, work_dir)

            engine = SyncEngine(tar_path.parent)
            ungoogled_only = config.get_ungoogled_only_extensions()
            ext_restrictions = config.get_extension_browser_restrictions()

            for b in target_browsers:
                if not b.is_installed():
                    print(f"{b.name}: not installed — skipping")
                    continue
                profiles = b.discover_profiles()
                if not profiles:
                    print(f"{b.name}: no profiles found — skipping")
                    continue
                for profile_path in profiles:
                    print(f"Restoring {b.name}/{profile_path.name}...")
                    engine.restore_profile_from_backup(
                        profile_path, work_dir,
                        browser=b,
                        on_progress=lambda msg: print(f"  {msg}"),
                    )
                    _extensions.install_external_extensions(
                        work_dir, b,
                        ungoogled_only_ext_ids=ungoogled_only,
                        browser_restrictions=ext_restrictions,
                    )
            print("Restore complete.")
        finally:
            shutil.rmtree(work_dir)
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
