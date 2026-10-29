from __future__ import annotations

import argparse
import json
import shutil

from src import config
from src.browsers import ALL_BROWSERS, get_browser
from src.operations import operation_lock
from src.sync_engine import SyncEngine

COMMANDS = {"browsers", "sync", "restore", "config", "remove-profile"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Chromium Profile Syncer CLI")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("browsers", help="List browsers and profiles")
    for name in ("sync", "restore"):
        command = commands.add_parser(name)
        command.add_argument("--browser", required=name == "restore")
        command.add_argument("--profile", required=name == "restore")
        if name == "sync":
            command.add_argument("--direction", choices=("push", "pull", "both"))
    remove = commands.add_parser("remove-profile", help="Delete one local profile; keep backup")
    remove.add_argument("--browser", required=True)
    remove.add_argument("--profile", required=True)
    remove.add_argument("--yes", action="store_true", required=True)
    settings = commands.add_parser("config").add_subparsers(dest="setting", required=True)
    settings.add_parser("show")
    folder = settings.add_parser("set-sync-folder")
    folder.add_argument("path")
    for name in ("set-browser", "set-profile"):
        command = settings.add_parser(name)
        command.add_argument("--browser", required=True)
        command.add_argument("--enabled", choices=("true", "false"), required=True)
        if name == "set-profile":
            command.add_argument("--profile", required=True)
            command.add_argument("--direction", choices=("push", "pull", "both"), default="both")
    return parser


def run(argv: list[str]) -> int:
    from pathlib import Path

    args = build_parser().parse_args(argv)
    try:
        with operation_lock():
            if args.command == "browsers":
                result = [
                    {
                        "name": browser.name,
                        "installed": browser.is_installed(),
                        "running": browser.is_running(),
                        "profiles": [path.name for path in browser.discover_profiles()],
                    }
                    for browser in ALL_BROWSERS
                ]
            elif args.command in ("sync", "restore"):
                folder = config.get_sync_folder()
                if folder is None:
                    raise ValueError("Configure a sync folder first")
                if args.browser and get_browser(args.browser) is None:
                    raise ValueError(f"Unknown browser: {args.browser}")
                if args.browser:
                    args.browser = get_browser(args.browser).name
                if args.profile and args.browser:
                    browser = get_browser(args.browser)
                    if args.profile not in {p.name for p in browser.discover_profiles()}:
                        raise ValueError(f"Profile not found: {args.profile}")
                result = SyncEngine(folder).sync_all(
                    only_browser=args.browser,
                    only_profile=args.profile,
                    force_direction="pull" if args.command == "restore" else args.direction,
                    automatic=False,
                )
            elif args.command == "remove-profile":
                browser = get_browser(args.browser)
                if browser is None:
                    raise ValueError(f"Unknown browser: {args.browser}")
                if browser.is_running():
                    raise OSError(f"Close {browser.name} before removing a profile")
                profile = next(
                    (p for p in browser.discover_profiles() if p.name == args.profile), None
                )
                if profile is None:
                    raise ValueError(f"Profile not found: {args.profile}")
                shutil.rmtree(profile)
                config.remove_profile(browser.name, profile.name, profile)
                result = {"removed": str(profile), "shared_backup_preserved": True}
            elif args.setting == "show":
                result = config.load()
            elif args.setting == "set-sync-folder":
                config.set_sync_folder(Path(args.path).expanduser().resolve())
                result = {"ok": True}
            else:
                browser = get_browser(args.browser)
                if browser is None:
                    raise ValueError(f"Unknown browser: {args.browser}")
                enabled = args.enabled == "true"

                def change(data):
                    if args.setting == "set-browser":
                        data.setdefault("enabled_browsers", {})[browser.name] = enabled
                        return
                    if args.profile not in {p.name for p in browser.discover_profiles()}:
                        raise ValueError(f"Profile not found: {args.profile}")
                    profiles = data.setdefault("enabled_profiles", {})
                    names = set(profiles.get(browser.name, []))
                    names.add(args.profile) if enabled else names.discard(args.profile)
                    profiles[browser.name] = sorted(names)
                    data.setdefault("profile_directions", {}).setdefault(browser.name, {})[
                        args.profile
                    ] = args.direction

                config.mutate(change)
                result = {"ok": True}
            print(json.dumps(result))
        return 0
    except (OSError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 1
