from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "chromium-profile-syncer"


def _exe_args() -> list[str]:
    """Return the command args to launch this app with --tray flag.

    In a PyInstaller frozen bundle, sys.executable is the bundle itself.
    In dev mode, we need the interpreter plus the script path.
    Always includes --tray for autostart (launches in tray-only mode).
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--tray"]
    return [sys.executable, str(Path(sys.argv[0]).resolve()), "--tray"]


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
        desktop_content = f"""[Desktop Entry]
Type=Application
Name={APP_NAME}
Exec={exec_cmd}
Hidden=false
X-GNOME-Autostart-enabled=true
"""
        desktop_path.parent.mkdir(parents=True, exist_ok=True)
        desktop_path.write_text(desktop_content, encoding="utf-8")
    else:
        desktop_path.unlink(missing_ok=True)
