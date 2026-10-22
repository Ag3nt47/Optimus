"""One interactive GUI per desktop user and canonical Optimus data directory.

The GUI lease is separate from the maintenance lease: read-only commands do not
participate, and older Optimus releases still need the maintenance lease.
"""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import sys
import time

from PySide6.QtCore import QLockFile
from PySide6.QtNetwork import QLocalServer, QLocalSocket


def _user_identity():
    if os.name != 'nt':
        return 'uid:' + str(os.getuid())
    # Profile/environment paths can differ across an MSIX/native/UAC restart.
    # A Windows token SID identifies the account independently of those paths.
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    security = ctypes.WinDLL('advapi32', use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    security.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    security.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                            wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    security.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise OSError('The desktop account identity could not be read.')
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        if not 0 < size.value < 65536:
            raise OSError('The desktop account identity is unavailable.')
        info = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 1, info, size, ctypes.byref(size)):
            raise OSError('The desktop account identity could not be read.')
        sid = ctypes.cast(info, ctypes.POINTER(ctypes.c_void_p)).contents.value
        value = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid, ctypes.byref(value)):
            raise OSError('The desktop account identity could not be resolved.')
        try:
            return 'sid:' + value.value
        finally:
            kernel.LocalFree(ctypes.cast(value, ctypes.c_void_p))
    finally:
        kernel.CloseHandle(token)


def instance_key(data_dir, *, user=None):
    """Keep source workspaces and elevated launches in the same GUI scope."""
    from .filesystem import resolve_data_root
    root = os.path.normcase(str(resolve_data_root(Path(data_dir))))
    identity = user if user is not None else _user_identity()
    return hashlib.sha256((str(identity) + '\0' + root).encode('utf-8')).hexdigest()


class GuiInstance:
    def __init__(self, data_dir):
        self.key = instance_key(data_dir)
        # macOS places sockets under a long /var/folders/.../T prefix and has
        # a 104-byte Unix socket path limit. Keep 128 bits in the endpoint name.
        self.name = 'Optimus.' + self.key[:32]
        self.lock = QLockFile(str(Path(data_dir) / ('.gui-' + self.key + '.lock')))
        # Never expire a lease merely because a GUI or a Windows dialog is busy.
        # Qt can still recover a lease whose owning process has actually exited.
        self.lock.setStaleLockTime(0)
        self.owned = False
        self.server = None
        self._sockets = set()

    def try_claim(self):
        if not self.owned:
            self.owned = self.lock.tryLock(0)
        return self.owned

    def wait_for_handoff(self, timeout_ms=15000):
        """An approved UAC child waits until its launching parent releases it."""
        deadline = time.monotonic() + timeout_ms / 1000
        while not self.try_claim():
            if time.monotonic() >= deadline:
                return False
            time.sleep(.05)
        return True

    def listen(self, activate):
        if not self.owned:
            raise RuntimeError('The GUI lease is not owned by this process.')
        server = QLocalServer()
        server.setSocketOptions(QLocalServer.UserAccessOption)
        # A crashed primary can leave a Unix socket. Only the lease owner may
        # remove that endpoint; a live owner's lock is never removed here.
        QLocalServer.removeServer(self.name)
        if not server.listen(self.name):
            raise RuntimeError('The Optimus activation endpoint could not start: ' + server.errorString())
        self.server = server

        def accept():
            while server.hasPendingConnections():
                socket = server.nextPendingConnection()
                self._sockets.add(socket)
                socket.disconnected.connect(lambda connection=socket: self._forget(connection))

                def read(connection=socket):
                    if connection.bytesAvailable() > 64:
                        connection.abort()
                        return
                    if not connection.canReadLine():
                        return
                    request = bytes(connection.readLine(65))
                    if request != b'activate\n':
                        connection.abort()
                        return
                    try:
                        activated = activate() is not False
                    except (RuntimeError, OSError):
                        activated = False
                    connection.write(b'activated\n' if activated else b'unavailable\n')
                    connection.flush()
                    connection.disconnectFromServer()
                socket.readyRead.connect(read)
                read()
        server.newConnection.connect(accept)

    def _forget(self, socket):
        self._sockets.discard(socket)
        socket.deleteLater()

    def activate_existing(self, timeout_ms=3000):
        """A duplicate cannot build a GUI if activation fails or is unavailable."""
        if sys.platform == 'win32':
            try:
                pid, _, _ = self.lock.getLockInfo()
                if pid > 0:
                    # Delegate only the normal foreground grant from this launch.
                    ctypes.windll.user32.AllowSetForegroundWindow(pid)
            except (AttributeError, OSError, TypeError, ValueError):
                pass
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            socket = QLocalSocket()
            socket.connectToServer(self.name)
            remaining = max(1, int((deadline - time.monotonic()) * 1000))
            if socket.waitForConnected(min(remaining, 200)):
                socket.write(b'activate\n')
                socket.flush()
                remaining = max(1, int((deadline - time.monotonic()) * 1000))
                if socket.waitForReadyRead(remaining):
                    response = bytes(socket.readLine(65))
                    socket.abort()
                    return response == b'activated\n'
            socket.abort()
            time.sleep(.05)
        return False

    def close(self):
        if self.server is not None:
            self.server.close()
            self.server = None
        for socket in tuple(self._sockets):
            socket.abort()
        self._sockets.clear()
        if self.owned:
            self.lock.unlock()
            self.owned = False
