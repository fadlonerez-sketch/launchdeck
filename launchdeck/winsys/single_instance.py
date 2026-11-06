"""Single-instance guard: a named mutex decides ownership, a local socket forwards requests."""

from __future__ import annotations

import ctypes
import hashlib
import logging
import os
from ctypes import wintypes

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

log = logging.getLogger(__name__)

ERROR_ALREADY_EXISTS = 183


def instance_name(data_dir: str) -> str:
    """Unique per user session *and* data directory (so isolated test instances don't collide)."""
    digest = hashlib.sha1(os.path.normcase(os.path.abspath(data_dir)).encode("utf-8")).hexdigest()[:12]
    return f"LaunchDeck-{digest}"


class SingleInstance(QObject):
    message_received = Signal(str)

    def __init__(self, name: str, parent: QObject | None = None):
        super().__init__(parent)
        self.name = name
        self._mutex = None
        self._server: QLocalServer | None = None

    def acquire(self) -> bool:
        """Return True if this process is the primary instance."""
        if os.name == "nt":
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            self._mutex = kernel32.CreateMutexW(None, False, f"Local\\{self.name}")
            if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
                return False
        QLocalServer.removeServer(self.name)  # clears a stale endpoint left by a crash
        self._server = QLocalServer(self)
        self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
        if not self._server.listen(self.name):
            log.warning("Single-instance server could not listen: %s", self._server.errorString())
        self._server.newConnection.connect(self._on_connection)
        return True

    def _on_connection(self) -> None:
        while self._server is not None and self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            sock.readyRead.connect(lambda s=sock: self._read(s))
            sock.disconnected.connect(sock.deleteLater)

    def _read(self, sock: QLocalSocket) -> None:
        text = bytes(sock.readAll()).decode("utf-8", "replace").strip()
        if text in ("show", "launcher", "dashboard", "organizer", "settings", "activity", "quit"):
            self.message_received.emit(text)
        sock.disconnectFromServer()

    def send(self, message: str, timeout_ms: int = 2000) -> bool:
        """Forward a request to the primary instance (called from a secondary process)."""
        sock = QLocalSocket()
        sock.connectToServer(self.name)
        if not sock.waitForConnected(timeout_ms):
            return False
        sock.write(message.encode("utf-8"))
        ok = sock.waitForBytesWritten(timeout_ms)
        sock.disconnectFromServer()
        return ok

    def release(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        if self._mutex and os.name == "nt":
            ctypes.WinDLL("kernel32").CloseHandle(wintypes.HANDLE(self._mutex))
            self._mutex = None
