from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(autouse=True)
def isolated_app_state(tmp_path, monkeypatch, request):
    from src import autostart, config

    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "app")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "app" / "config.json")
    if request.path.name != "test_autostart.py":
        monkeypatch.setattr(autostart, "apply", lambda enabled: None)


@pytest.fixture(scope="session")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])
