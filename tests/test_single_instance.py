from __future__ import annotations

import subprocess
import sys

from PySide6.QtTest import QSignalSpy

from src import config
from src.single_instance import SingleInstance


def _send_from_process(command):
    return subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; from pathlib import Path; from src import config; "
            "from src.single_instance import send_command; "
            "config.CONFIG_PATH=Path(sys.argv[1]); sys.exit(0 if send_command(sys.argv[2]) else 1)",
            str(config.CONFIG_PATH),
            command,
        ]
    )


def test_only_one_instance_can_acquire_the_gui_lock(qapp):
    first, second = SingleInstance(), SingleInstance()
    try:
        assert first.acquire()
        assert not second.acquire(show_existing=False)
    finally:
        second.close()
        first.close()


def test_second_launch_can_show_settings_over_local_socket(qapp):
    first = SingleInstance()
    try:
        assert first.acquire()
        shown = QSignalSpy(first.show_requested)
        with _send_from_process("show") as sender:
            assert shown.wait(5000)
            assert sender.wait(timeout=5) == 0
        assert shown.count() == 1
    finally:
        first.close()


def test_quit_request_and_unknown_command(qapp):
    instance = SingleInstance()
    try:
        assert instance.acquire()
        quitting = QSignalSpy(instance.quit_requested)
        with _send_from_process("quit") as sender:
            assert quitting.wait(5000)
            assert sender.wait(timeout=5) == 0
        assert quitting.count() == 1
    finally:
        instance.close()
