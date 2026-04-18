#!/usr/bin/env python3
"""Non-interactive CLI for chromium-profile-syncer.

Usage:
  uv run python cli.py browsers
  uv run python cli.py sync [--browser NAME] [--profile NAME] [--direction push|pull|both]
  uv run python cli.py restore --browser NAME [--profile NAME]
  uv run python cli.py config show
  uv run python cli.py config set-sync-folder PATH
  uv run python cli.py config set-browser NAME --enabled true|false
  uv run python cli.py config set-profile --browser NAME --profile NAME
      [--enabled true|false] [--direction push|pull|both]

Progress messages go to stderr. JSON results go to stdout. Exit 0 on success, 1 on error.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


def _progress(msg: str) -> None:
    print(f"[progress] {msg}", file=sys.stderr, flush=True)


def cmd_browsers(_args: argparse.Namespace) -> int:
    from src.browsers import ALL_BROWSERS

    result = []
    for browser in ALL_BROWSERS:
        installed = browser.is_installed()
        entry: dict = {
            "name": browser.name,
            "installed": installed,
            "running": browser.is_running() if installed else False,
            "profiles": [],
        }
        if installed:
            entry["profiles"] = [
                {"path": p.name, "display_name": browser.get_profile_name(p)}
                for p in browser.discover_profiles()
            ]
        result.append(entry)
    print(json.dumps(result, indent=2))
    return 0


def _require_sync_folder() -> Path:
    import src.config as config

    sf = config.get_sync_folder()
    if not sf:
        print(json.dumps({"error": "sync_folder not configured",
                          "hint": "run: cli.py config set-sync-folder PATH"}))
        sys.exit(1)
    return sf


def cmd_sync(args: argparse.Namespace) -> int:
    from src.sync_engine import SyncEngine

    engine = SyncEngine(_require_sync_folder())
    result = engine.sync_all(
        only_browser=args.browser or None,
        only_profile=args.profile or None,
        force_direction=args.direction or None,
        on_progress=_progress,
    )
    print(json.dumps(result))
    return 0


def cmd_restore(args: argparse.Namespace) -> int:
    import shutil
    import tempfile

    import src.config as config
    from src.browsers import ALL_BROWSERS
    from src.sync import archive as _archive
    from src.sync.extensions import install_external_extensions
    from src.sync_engine import SyncEngine

    browser = next((b for b in ALL_BROWSERS if b.name == args.browser), None)
    if browser is None:
        known = [b.name for b in ALL_BROWSERS]
        print(json.dumps({"error": f"Unknown browser: {args.browser!r}", "known": known}))
        return 1

    sf = _require_sync_folder()
    archive_path = sf / _archive.ARCHIVE_NAME
    if not archive_path.exists():
        print(json.dumps({"error": f"No {_archive.ARCHIVE_NAME} in sync folder"}))
        return 1

    profiles = browser.discover_profiles()
    if args.profile:
        profiles = [p for p in profiles if p.name == args.profile]
    if not profiles:
        msg = (
            f"Profile {args.profile!r} not found for {args.browser}"
            if args.profile
            else f"No profiles found for {args.browser}"
        )
        print(json.dumps({"error": msg}))
        return 1

    work_dir = Path(tempfile.mkdtemp(prefix="cps-restore-"))
    try:
        _progress("Unpacking archive...")
        _archive.unpack_archive(archive_path, work_dir)
        engine = SyncEngine(sf)
        ungoogled_only_ext_ids = config.get_ungoogled_only_extensions()
        ext_restrictions = config.get_extension_browser_restrictions()
        aliases = browser.ext_id_aliases
        for profile_path in profiles:
            _progress(f"{browser.name}/{profile_path.name}")
            if aliases:
                engine._translate_ext_aliases(work_dir, aliases, to_alias=True)
            try:
                engine.restore_profile_from_backup(
                    profile_path, work_dir, browser=browser, on_progress=_progress,
                )
            finally:
                if aliases:
                    engine._translate_ext_aliases(work_dir, aliases, to_alias=False)
            install_external_extensions(
                work_dir, browser,
                ungoogled_only_ext_ids=ungoogled_only_ext_ids,
                browser_restrictions=ext_restrictions,
            )
    finally:
        shutil.rmtree(work_dir)

    print(json.dumps({"ok": True, "browser": args.browser, "profiles": [p.name for p in profiles]}))
    return 0


def cmd_config_show(_args: argparse.Namespace) -> int:
    import src.config as config

    print(json.dumps(config.load(), indent=2))
    return 0


def cmd_config_set_sync_folder(args: argparse.Namespace) -> int:
    import src.config as config

    p = Path(args.path)
    config.set_sync_folder(p)
    print(json.dumps({"ok": True, "sync_folder": str(p)}))
    return 0


def cmd_config_set_browser(args: argparse.Namespace) -> int:
    import src.config as config
    from src.browsers import ALL_BROWSERS

    known = {b.name for b in ALL_BROWSERS}
    if args.name not in known:
        print(json.dumps({"error": f"Unknown browser: {args.name!r}", "known": sorted(known)}))
        return 1

    enabled = args.enabled.lower() not in ("false", "0", "no")
    browsers = config.get_enabled_browsers()
    browsers[args.name] = enabled
    config.set_enabled_browsers(browsers)
    print(json.dumps({"ok": True, "browser": args.name, "enabled": enabled}))
    return 0


def cmd_config_set_profile(args: argparse.Namespace) -> int:
    import src.config as config
    from src.browsers import ALL_BROWSERS

    known = {b.name for b in ALL_BROWSERS}
    if args.browser not in known:
        print(json.dumps({"error": f"Unknown browser: {args.browser!r}", "known": sorted(known)}))
        return 1

    profiles = config.get_enabled_profiles()
    browser_profiles: list[str] = list(profiles.get(args.browser, []))

    if args.enabled is not None:
        enabled = args.enabled.lower() not in ("false", "0", "no")
        if enabled and args.profile not in browser_profiles:
            browser_profiles.append(args.profile)
        elif not enabled and args.profile in browser_profiles:
            browser_profiles.remove(args.profile)
        profiles[args.browser] = browser_profiles
        config.set_enabled_profiles(profiles)

    if args.direction is not None:
        if args.direction not in ("push", "pull", "both"):
            print(json.dumps({"error": "direction must be push, pull, or both"}))
            return 1
        directions = config.get_profile_directions()
        directions.setdefault(args.browser, {})[args.profile] = args.direction
        config.set_profile_directions(directions)

    print(json.dumps({"ok": True, "browser": args.browser, "profile": args.profile}))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cli.py",
        description="Non-interactive CLI for chromium-profile-syncer",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("browsers", help="List all browsers and their profiles")

    p_sync = sub.add_parser("sync", help="Sync profiles (push/pull/both)")
    p_sync.add_argument("--browser", metavar="NAME", help="Limit to this browser")
    p_sync.add_argument("--profile", metavar="NAME", help="Limit to profile dir (e.g. Default)")
    p_sync.add_argument("--direction", choices=["push", "pull", "both"], help="Override direction")

    p_restore = sub.add_parser("restore", help="Restore profile from cloud backup (full overwrite)")
    p_restore.add_argument("--browser", required=True, metavar="NAME")
    p_restore.add_argument("--profile", metavar="NAME", help="Profile dir; omit = all")

    p_cfg = sub.add_parser("config", help="Show or modify configuration")
    cfg_sub = p_cfg.add_subparsers(dest="config_command", required=True)

    cfg_sub.add_parser("show", help="Print current config as JSON")

    p_sf = cfg_sub.add_parser("set-sync-folder", help="Set cloud sync folder path")
    p_sf.add_argument("path", metavar="PATH")

    p_sb = cfg_sub.add_parser("set-browser", help="Enable or disable a browser")
    p_sb.add_argument("name", metavar="NAME")
    p_sb.add_argument("--enabled", required=True, metavar="true|false")

    p_sp = cfg_sub.add_parser("set-profile", help="Add/remove a profile or set its sync direction")
    p_sp.add_argument("--browser", required=True, metavar="NAME")
    p_sp.add_argument("--profile", required=True, metavar="NAME")
    p_sp.add_argument("--enabled", metavar="true|false")
    p_sp.add_argument("--direction", choices=["push", "pull", "both"])

    return parser


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    parser = build_parser()
    args = parser.parse_args()

    dispatch = {
        "browsers": cmd_browsers,
        "sync": cmd_sync,
        "restore": cmd_restore,
    }

    if args.command == "config":
        cfg_dispatch = {
            "show": cmd_config_show,
            "set-sync-folder": cmd_config_set_sync_folder,
            "set-browser": cmd_config_set_browser,
            "set-profile": cmd_config_set_profile,
        }
        fn = cfg_dispatch.get(args.config_command)
    else:
        fn = dispatch.get(args.command)

    if fn is None:
        parser.print_help()
        sys.exit(1)

    sys.exit(fn(args) or 0)


if __name__ == "__main__":
    main()
