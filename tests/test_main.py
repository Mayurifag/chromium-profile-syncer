from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch


def test_tray_flag_prevents_settings_dialog():
    """With --tray flag, settings dialog should not open on startup."""
    import src.main

    with patch.object(sys, "argv", ["main.py", "--tray"]), \
         patch("src.main.single_instance.acquire"), \
         patch("src.main.single_instance.setup_signal_handler"), \
         patch("src.main.QApplication") as mock_app, \
         patch("src.main.TrayApp") as mock_tray, \
         patch("src.main.SyncEngine"), \
         patch("src.main.config.get_sync_folder", return_value=None), \
         patch("src.main.config.get_enabled_profiles", return_value={}), \
         patch("src.main.autostart.apply"), \
         patch("PySide6.QtCore.QTimer") as mock_timer:

        app_instance = MagicMock()
        mock_app.return_value = app_instance
        app_instance.exec.return_value = 0

        tray_instance = MagicMock()
        mock_tray.return_value = tray_instance

        try:
            src.main.main()
        except SystemExit:
            pass

        # QTimer.singleShot should NOT be called when --tray is present
        mock_timer.singleShot.assert_not_called()


def test_no_tray_flag_opens_settings_dialog():
    """Without --tray flag, settings dialog should open on startup."""
    import src.main

    with patch.object(sys, "argv", ["main.py"]), \
         patch("src.main.single_instance.acquire"), \
         patch("src.main.single_instance.setup_signal_handler"), \
         patch("src.main.QApplication") as mock_app, \
         patch("src.main.TrayApp") as mock_tray, \
         patch("src.main.SyncEngine"), \
         patch("src.main.config.get_sync_folder", return_value=None), \
         patch("src.main.config.get_enabled_profiles", return_value={}), \
         patch("src.main.autostart.apply"), \
         patch("PySide6.QtCore.QTimer") as mock_timer:

        app_instance = MagicMock()
        mock_app.return_value = app_instance
        app_instance.exec.return_value = 0

        tray_instance = MagicMock()
        mock_tray.return_value = tray_instance

        try:
            src.main.main()
        except SystemExit:
            pass

        # QTimer.singleShot should be called to open settings
        mock_timer.singleShot.assert_called_once()
        # Verify it was called with tray.open_settings
        call_args = mock_timer.singleShot.call_args
        assert call_args[0][0] == 0  # delay is 0
        assert call_args[0][1] == tray_instance.open_settings
