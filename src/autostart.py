from __future__ import annotations

import logging
import os
import stat
import sys
from pathlib import Path

APP_NAME = "chromium-profile-syncer"

_LOG = logging.getLogger(__name__)


def _linux_installed_binary() -> Path | None:
    candidate = Path.home() / ".local" / "bin" / APP_NAME
    return candidate if candidate.is_file() else None


def _exe_args() -> list[str]:
    """Return the command args to launch this app with --tray flag.

    On Linux, prefer the installed binary at ~/.local/bin so autostart
    survives venv churn even when toggled from a dev-mode session.
    """
    if sys.platform.startswith("linux"):
        installed = _linux_installed_binary()
        if installed is not None:
            return [str(installed), "--tray"]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--tray"]
    return [sys.executable, str(Path(sys.argv[0]).resolve()), "--tray"]


def _linux_icon_path() -> Path:
    return Path.home() / ".local" / "share" / APP_NAME / "icon.svg"


def _write_linux_icon() -> Path:
    from src.dracula import APP_ICON_SVG

    icon_path = _linux_icon_path()
    icon_path.parent.mkdir(parents=True, exist_ok=True)
    icon_path.write_text(APP_ICON_SVG, encoding="utf-8")
    return icon_path


def apply(enabled: bool) -> None:
    """Register or deregister autostart for the current platform."""
    if sys.platform == "win32":
        _windows(enabled)
    elif sys.platform == "darwin":
        _macos(enabled)
    else:
        _linux(enabled)


def _windows(enabled: bool) -> None:
    import winreg

    key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        key_path,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            cmd = " ".join(_exe_args())
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def _macos(enabled: bool) -> None:
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"com.{APP_NAME}.plist"
    if enabled:
        args = _exe_args()
        program_args = "\n".join(f"        <string>{a}</string>" for a in args)
        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.{APP_NAME}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <false/>
</dict>
</plist>
"""
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_path.write_text(plist_content, encoding="utf-8")
    else:
        plist_path.unlink(missing_ok=True)


def _linux(enabled: bool) -> None:
    xdg_config = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    desktop_path = Path(xdg_config) / "autostart" / f"{APP_NAME}.desktop"
    if enabled:
        args = _exe_args()
        exec_cmd = " ".join(f'"{a}"' if " " in a else a for a in args)
        icon_path = _write_linux_icon()
        desktop_content = f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Comment=Sync Chromium profiles across machines
Exec={exec_cmd}
Icon={icon_path}
Terminal=false
Categories=Utility;
StartupNotify=false
Hidden=false
X-GNOME-Autostart-enabled=true
X-GNOME-Autostart-Delay=10
"""
        desktop_path.parent.mkdir(parents=True, exist_ok=True)
        desktop_path.write_text(desktop_content, encoding="utf-8")
        desktop_path.chmod(
            stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
            | stat.S_IRGRP | stat.S_IXGRP
            | stat.S_IROTH | stat.S_IXOTH
        )
        _LOG.info("Autostart enabled: %s exec=%s", desktop_path, exec_cmd)
    else:
        desktop_path.unlink(missing_ok=True)
        _LOG.info("Autostart disabled: %s", desktop_path)
