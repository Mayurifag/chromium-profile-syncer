from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import psutil

from src.single_instance import _terminate


def _make_proc(name: str) -> MagicMock:
    proc = MagicMock()
    proc.name.return_value = name
    return proc


def test_terminate_kills_matching_process():
    proc = _make_proc("chromium-profile-syncer")
    with patch("src.single_instance.psutil.Process", return_value=proc):
        _terminate(1234)
    proc.terminate.assert_called_once()


def test_terminate_kills_python_process():
    proc = _make_proc("python")
    with patch("src.single_instance.psutil.Process", return_value=proc):
        _terminate(1234)
    proc.terminate.assert_called_once()


def test_terminate_kills_python3_process():
    proc = _make_proc("python3")
    with patch("src.single_instance.psutil.Process", return_value=proc):
        _terminate(1234)
    proc.terminate.assert_called_once()


def test_terminate_skips_unrelated_process(caplog):
    proc = _make_proc("firefox")
    with patch("src.single_instance.psutil.Process", return_value=proc):
        with caplog.at_level(logging.WARNING, logger="src.single_instance"):
            _terminate(1234)
    proc.terminate.assert_not_called()
    assert "not our process" in caplog.text


def test_terminate_handles_no_such_process():
    with patch("src.single_instance.psutil.Process", side_effect=psutil.NoSuchProcess(1234)):
        _terminate(1234)  # must not raise


def test_terminate_handles_access_denied():
    with patch("src.single_instance.psutil.Process", side_effect=psutil.AccessDenied(1234)):
        _terminate(1234)  # must not raise
