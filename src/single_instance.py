from __future__ import annotations

import atexit
import importlib.metadata
import logging
import os
import signal
import socket
import sys
from collections.abc import Callable
from pathlib import Path

import psutil

from src.config import CONFIG_DIR

_LOG = logging.getLogger(__name__)

_LOCK_FILE: Path = CONFIG_DIR / "app.lock"

try:
    _VERSION: str = importlib.metadata.version("chromium-profile-syncer")
except importlib.metadata.PackageNotFoundError:
    _VERSION = "dev"


def _read_lock() -> tuple[int, str] | None:
    """Return (pid, version) from the lock file, or None if missing/corrupt."""
    try:
        parts = _LOCK_FILE.read_text(encoding="utf-8").strip().split(":", 1)
        if len(parts) == 2:
            return int(parts[0]), parts[1]
    except (FileNotFoundError, ValueError, OSError):
        pass
    return None


def _write_lock() -> None:
    _LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LOCK_FILE.write_text(f"{os.getpid()}:{_VERSION}", encoding="utf-8")
    _LOG.debug("Lock written: pid=%d version=%s", os.getpid(), _VERSION)


def _remove_lock() -> None:
    try:
        _LOCK_FILE.unlink()
    except FileNotFoundError:
        pass


def _process_running(pid: int) -> bool:
    try:
        return psutil.pid_exists(pid) and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False


def _signal_existing(pid: int) -> None:
    if sys.platform == "win32":
        _LOG.warning("Cannot signal existing instance on Windows — doing nothing")
    else:
        try:
            os.kill(pid, signal.SIGUSR1)
        except (ProcessLookupError, PermissionError) as exc:
            _LOG.warning("Could not signal pid %d: %s", pid, exc)


def _terminate(pid: int) -> None:
    try:
        proc = psutil.Process(pid)
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except psutil.TimeoutExpired:
            proc.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
        _LOG.warning("Could not terminate pid %d: %s", pid, exc)


def acquire() -> None:
    """Enforce single-instance policy before Qt starts.

    - Same version running  → signal it to open settings, then sys.exit(0).
    - Different version running → terminate old process, then continue.
    - No running instance  → continue normally.

    Writes a fresh lock file and registers cleanup on process exit.
    """
    existing = _read_lock()
    if existing is not None:
        pid, version = existing
        if _process_running(pid):
            if version == _VERSION:
                _LOG.info("Same version already running (pid=%d) — raising it", pid)
                _signal_existing(pid)
                sys.exit(0)
            else:
                _LOG.info(
                    "Different version running (pid=%d ver=%s) — terminating it", pid, version
                )
                _terminate(pid)
        else:
            _LOG.debug("Stale lock (pid=%d) — ignoring", pid)

    _write_lock()
    atexit.register(_remove_lock)


def setup_signal_handler(open_settings_callback: Callable[[], None]) -> None:
    """Register SIGUSR1 → open_settings_callback.

    Uses a self-pipe + QSocketNotifier so the signal is delivered inside Qt's
    event loop without blocking, avoiding the "signal in non-main thread" issue.
    Must be called after QApplication is created.
    """
    if sys.platform == "win32":
        return

    from PySide6.QtCore import QSocketNotifier
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()

    rsock, wsock = socket.socketpair()
    rsock.setblocking(False)
    wsock.setblocking(False)

    def _py_handler(signum, frame):  # noqa: ARG001
        try:
            wsock.send(b"\x00")
        except OSError:
            pass

    signal.signal(signal.SIGUSR1, _py_handler)

    notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Type.Read, app)

    def _on_activated():
        try:
            rsock.recv(1)
        except OSError:
            pass
        _LOG.info("SIGUSR1 received — opening settings")
        open_settings_callback()

    notifier.activated.connect(_on_activated)

    # Keep sockets and notifier alive for the lifetime of the app
    app._si_socks = (rsock, wsock)
    app._si_notifier = notifier
