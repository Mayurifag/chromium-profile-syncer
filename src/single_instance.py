from __future__ import annotations

import hashlib

from PySide6.QtCore import QCoreApplication, QLockFile, QObject, Signal, Slot
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from src import config


def server_name() -> str:
    identity = str(config.CONFIG_PATH.resolve()).casefold().encode()
    return "chromium-profile-syncer-" + hashlib.sha256(identity).hexdigest()[:24]


def send_command(command: str, timeout: int = 1500) -> bool:
    _application = QCoreApplication.instance() or QCoreApplication([])
    socket = QLocalSocket()
    socket.connectToServer(server_name())
    if not socket.waitForConnected(timeout):
        return False
    socket.write((command + "\n").encode("ascii"))
    if socket.bytesToWrite() and not socket.waitForBytesWritten(timeout):
        socket.abort()
        return False
    socket.disconnectFromServer()
    return True


class SingleInstance(QObject):
    show_requested = Signal()
    quit_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        self.lock = QLockFile(str(config.CONFIG_PATH.with_name("gui.lock")))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self._connections: set[QLocalSocket] = set()
        self.server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        self.server.newConnection.connect(self._accept)

    def acquire(self, *, show_existing: bool = True) -> bool:
        if not self.lock.tryLock(0):
            if show_existing and not send_command("show"):
                raise OSError(
                    "App is already running but its settings window could not be contacted"
                )
            return False
        if not self.server.listen(server_name()):
            QLocalServer.removeServer(server_name())
            if not self.server.listen(server_name()):
                self.lock.unlock()
                raise OSError(self.server.errorString())
        return True

    @Slot()
    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self._connections.add(socket)
            socket.setReadBufferSize(64)
            socket.readyRead.connect(self._read_ready)
            socket.disconnected.connect(self._disconnected)
            self._read(socket)

    @Slot()
    def _read_ready(self) -> None:
        self._read(self.sender())

    @Slot()
    def _disconnected(self) -> None:
        socket = self.sender()
        self._connections.discard(socket)
        socket.deleteLater()

    def _read(self, socket: QLocalSocket) -> None:
        if not socket.canReadLine():
            return
        command = bytes(socket.readLine(64)).strip()
        socket.disconnectFromServer()
        if command == b"show":
            self.show_requested.emit()
        elif command == b"quit":
            self.quit_requested.emit()

    def close(self) -> None:
        self.server.close()
        for socket in list(self._connections):
            socket.readyRead.disconnect(self._read_ready)
            socket.disconnected.disconnect(self._disconnected)
            socket.abort()
            socket.deleteLater()
        self._connections.clear()
        self.lock.unlock()
