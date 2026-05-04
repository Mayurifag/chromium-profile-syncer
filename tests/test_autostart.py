from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# _exe_args
# ---------------------------------------------------------------------------

def test_exe_args_frozen():
    """In a frozen bundle, returns [executable, --tray] for autostart."""
    import src.autostart as autostart
    with patch.object(sys, "frozen", True, create=True), \
         patch.object(autostart, "_linux_installed_binary", return_value=None):
        result = autostart._exe_args()
    assert result == [sys.executable, "--tray"]


def test_exe_args_dev_mode(tmp_path):
    """In dev mode, returns [interpreter, script path, --tray] for autostart."""
    import src.autostart as autostart
    fake_script = str(tmp_path / "main.py")
    with patch.object(sys, "frozen", False, create=True), \
         patch.object(sys, "argv", [fake_script]), \
         patch.object(autostart, "_linux_installed_binary", return_value=None):
        result = autostart._exe_args()
    assert result == [sys.executable, str(Path(fake_script).resolve()), "--tray"]


def test_exe_args_linux_prefers_installed_binary(tmp_path):
    """On Linux, installed binary path overrides sys.executable."""
    import src.autostart as autostart
    installed = tmp_path / "chromium-profile-syncer"
    installed.write_text("#!/bin/sh\n", encoding="utf-8")
    with patch.object(sys, "platform", "linux"), \
         patch.object(autostart, "_linux_installed_binary", return_value=installed):
        result = autostart._exe_args()
    assert result == [str(installed), "--tray"]


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

def test_macos_enable(tmp_path):
    """Enable writes a plist with RunAtLoad and com.chromium-profile-syncer."""
    import src.autostart as autostart

    captured: dict[str, str] = {}

    def fake_write_text(self, text: str, encoding: str = "utf-8") -> None:
        captured["content"] = text

    with patch.object(Path, "mkdir"), \
         patch.object(Path, "write_text", fake_write_text), \
         patch.object(sys, "frozen", True, create=True):
        autostart._macos(True)

    content = captured["content"]
    assert "RunAtLoad" in content
    assert "com.chromium-profile-syncer" in content
    assert "<true/>" in content
    assert "<false/>" in content


def test_macos_disable():
    """Disable calls unlink(missing_ok=True)."""
    import src.autostart as autostart

    with patch.object(Path, "unlink") as mock_unlink:
        autostart._macos(False)

    mock_unlink.assert_called_once_with(missing_ok=True)


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------

def test_linux_enable(tmp_path):
    """Enable writes a .desktop file with X-GNOME-Autostart-enabled=true."""
    import src.autostart as autostart

    captured: dict[str, str] = {}
    desktop_target = tmp_path / "autostart" / "chromium-profile-syncer.desktop"

    real_write_text = Path.write_text

    def fake_write_text(self, text: str, encoding: str = "utf-8") -> None:
        if self == desktop_target:
            captured["content"] = text
            return
        real_write_text(self, text, encoding=encoding)

    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}), \
         patch.object(Path, "write_text", fake_write_text), \
         patch.object(Path, "chmod"), \
         patch.object(autostart, "_linux_installed_binary", return_value=None), \
         patch.object(sys, "frozen", True, create=True):
        autostart._linux(True)

    content = captured["content"]
    assert "X-GNOME-Autostart-enabled=true" in content
    assert "chromium-profile-syncer" in content
    assert "Hidden=false" in content
    assert "Terminal=false" in content
    assert "Icon=" in content
    assert "Categories=Utility;" in content
    assert "StartupNotify=false" in content
    assert "X-GNOME-Autostart-Delay=10" in content


def test_linux_disable(tmp_path):
    """Disable calls unlink(missing_ok=True)."""
    import src.autostart as autostart

    with patch.dict("os.environ", {"XDG_CONFIG_HOME": str(tmp_path)}), \
         patch.object(Path, "unlink") as mock_unlink:
        autostart._linux(False)

    mock_unlink.assert_called_once_with(missing_ok=True)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

def _make_winreg_mock():
    winreg = MagicMock()
    winreg.HKEY_CURRENT_USER = 0x80000001
    winreg.KEY_SET_VALUE = 0x0002
    winreg.REG_SZ = 1
    # OpenKey returns a context manager
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=ctx)
    ctx.__exit__ = MagicMock(return_value=False)
    winreg.OpenKey.return_value = ctx
    return winreg, ctx


def test_windows_enable():
    """Enable calls SetValueEx with APP_NAME."""
    import src.autostart as autostart

    winreg, ctx = _make_winreg_mock()

    with patch.dict("sys.modules", {"winreg": winreg}), \
         patch.object(sys, "frozen", True, create=True):
        autostart._windows(True)

    winreg.SetValueEx.assert_called_once()
    args = winreg.SetValueEx.call_args[0]
    assert args[1] == autostart.APP_NAME


def test_windows_disable():
    """Disable calls DeleteValue with APP_NAME."""
    import src.autostart as autostart

    winreg, ctx = _make_winreg_mock()

    with patch.dict("sys.modules", {"winreg": winreg}), \
         patch.object(sys, "frozen", True, create=True):
        autostart._windows(False)

    winreg.DeleteValue.assert_called_once()
    args = winreg.DeleteValue.call_args[0]
    assert args[1] == autostart.APP_NAME


def test_windows_disable_idempotent():
    """DeleteValue raising FileNotFoundError is silently ignored."""
    import src.autostart as autostart

    winreg, ctx = _make_winreg_mock()
    winreg.DeleteValue.side_effect = FileNotFoundError

    with patch.dict("sys.modules", {"winreg": winreg}), \
         patch.object(sys, "frozen", True, create=True):
        # Should not raise
        autostart._windows(False)


# ---------------------------------------------------------------------------
# apply() dispatch
# ---------------------------------------------------------------------------

def test_apply_dispatches_windows():
    import src.autostart as autostart

    with patch.object(autostart, "_windows") as mock_w, \
         patch.object(sys, "platform", "win32"):
        autostart.apply(True)

    mock_w.assert_called_once_with(True)


def test_apply_dispatches_macos():
    import src.autostart as autostart

    with patch.object(autostart, "_macos") as mock_m, \
         patch.object(sys, "platform", "darwin"):
        autostart.apply(False)

    mock_m.assert_called_once_with(False)


def test_apply_dispatches_linux():
    import src.autostart as autostart

    with patch.object(autostart, "_linux") as mock_l, \
         patch.object(sys, "platform", "linux"):
        autostart.apply(True)

    mock_l.assert_called_once_with(True)
