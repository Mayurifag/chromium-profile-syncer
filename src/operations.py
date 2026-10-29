from __future__ import annotations

import threading
from contextlib import contextmanager
from functools import wraps

from PySide6.QtCore import QLockFile

_THREAD_LOCK = threading.RLock()
_STATE = threading.local()


class OperationBusyError(OSError):
    pass


@contextmanager
def operation_lock():
    from src import config

    if not _THREAD_LOCK.acquire(blocking=False):
        raise OperationBusyError("Another operation is running. Wait for it to finish.")
    lock = None
    try:
        depth = getattr(_STATE, "depth", 0)
        if depth == 0:
            path = config.CONFIG_PATH.with_name("operations.lock")
            path.parent.mkdir(parents=True, exist_ok=True)
            lock = QLockFile(str(path))
            lock.setStaleLockTime(0)
            if not lock.tryLock(0):
                raise OperationBusyError("Another process is updating profiles or the backup.")
        _STATE.depth = depth + 1
        try:
            yield
        finally:
            _STATE.depth = depth
    finally:
        if lock is not None and lock.isLocked():
            lock.unlock()
        _THREAD_LOCK.release()


def locked(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with operation_lock():
            return function(*args, **kwargs)

    return wrapped
