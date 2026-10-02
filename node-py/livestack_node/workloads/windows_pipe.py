"""Bounded named-pipe transport for launch verification on Windows.

The Unix socket's role (openspec/changes/windows-host-worker, design
"Verifier"): a byte-mode, local-only pipe whose DACL admits SYSTEM and the
worker account. Every read and write is overlapped and waits at most the
caller's remaining deadline, then cancels, so a stalled peer cannot hold the
other side past the 5 s launch deadline. The object offers the socket methods
`launch_contract.receive` uses (settimeout, recv, sendall).
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import time
import _winapi

from . import windows_proc
from .model import WorkloadError

PIPE_PREFIX = '\\\\.\\pipe\\livestack-compilation-'
PIPE_ACCESS_DUPLEX = 0x3
FILE_FLAG_OVERLAPPED = 0x40000000
FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
PIPE_TYPE_BYTE, PIPE_READMODE_BYTE, PIPE_WAIT, PIPE_REJECT_REMOTE_CLIENTS = 0, 0, 0, 0x8
ERROR_IO_PENDING, ERROR_PIPE_BUSY, ERROR_PIPE_CONNECTED, ERROR_BROKEN_PIPE = 997, 231, 535, 109
ERROR_MORE_DATA = 234
WAIT_TIMEOUT = 258
MAX_INSTANCES = 16


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [('nLength', wintypes.DWORD), ('lpSecurityDescriptor', ctypes.c_void_p),
                ('bInheritHandle', wintypes.BOOL)]


class PipeStream:
    def __init__(self, handle):
        self.handle = handle
        self.timeout = None

    def settimeout(self, seconds):
        self.timeout = seconds

    def _wait(self, overlapped, what):
        ms = _winapi.INFINITE if self.timeout is None else max(0, int(self.timeout*1000))
        if _winapi.WaitForMultipleObjects([overlapped.event], False, ms) == WAIT_TIMEOUT:
            overlapped.cancel()
            overlapped.GetOverlappedResult(True)
            raise WorkloadError('compilation_verification_deadline', 503)
        count, error = overlapped.GetOverlappedResult(True)
        if error not in (0, ERROR_MORE_DATA):
            raise OSError(error, what+' failed')
        return count

    def recv(self, size):
        try:
            overlapped, error = _winapi.ReadFile(self.handle, size, overlapped=True)
        except OSError as failure:
            if failure.winerror == ERROR_BROKEN_PIPE:
                return b''
            raise
        if error not in (0, ERROR_IO_PENDING, ERROR_MORE_DATA):
            raise OSError(error, 'ReadFile failed')
        try:
            self._wait(overlapped, 'ReadFile')
        except OSError as failure:
            if getattr(failure, 'winerror', None) == ERROR_BROKEN_PIPE:
                return b''
            raise
        return overlapped.getbuffer()

    def sendall(self, data):
        overlapped, error = _winapi.WriteFile(self.handle, data, overlapped=True)
        if error not in (0, ERROR_IO_PENDING):
            raise OSError(error, 'WriteFile failed')
        if self._wait(overlapped, 'WriteFile') != len(data):
            raise OSError('short pipe write')

    def close(self):
        if self.handle is not None:
            _winapi.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def pipe_name(worker_sid_or_slot):
    return PIPE_PREFIX + worker_sid_or_slot


def connect(name, deadline):
    """The client end, refused unless the SERVER is a LocalSystem process: a
    same-account process that squats the name cannot answer for the verifier."""
    if not name.startswith(PIPE_PREFIX) or len(name) > 256:
        raise WorkloadError('compilation_verifier_slot_unavailable', 503)
    while True:
        try:
            handle = _winapi.CreateFile(name, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0, _winapi.NULL,
                                        _winapi.OPEN_EXISTING, FILE_FLAG_OVERLAPPED, _winapi.NULL)
            break
        except OSError as error:
            if error.winerror != ERROR_PIPE_BUSY or time.monotonic() >= deadline:
                raise
            time.sleep(.02)
    stream = PipeStream(handle)
    try:
        server = windows_proc.open_process(windows_proc.pipe_server_pid(handle))
        if server is None:
            raise WorkloadError('compilation_verifier_peer_untrusted', 403)
        try:
            if windows_proc.process_user_sid(server) != windows_proc.SYSTEM_SID:
                raise WorkloadError('compilation_verifier_peer_untrusted', 403)
        finally:
            windows_proc.close_handle(server)
    except BaseException:
        stream.close()
        raise
    return stream


class PipeListener:
    """Server ends of one pipe name, created by a LocalSystem service. The DACL
    admits SYSTEM and exactly one client account; the first instance refuses an
    existing name (a squatter), and remote clients are rejected."""

    def __init__(self, name, client_sid):
        self.name = name
        self.sddl = f'D:P(A;;GA;;;SY)(A;;GRGW;;;{client_sid})'
        self.first = True

    def _create(self):
        descriptor = windows_proc.security_descriptor(self.sddl)
        try:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            k = windows_proc._k32()
            k.CreateNamedPipeW.restype = wintypes.HANDLE
            k.CreateNamedPipeW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
            mode = PIPE_ACCESS_DUPLEX | FILE_FLAG_OVERLAPPED | (FILE_FLAG_FIRST_PIPE_INSTANCE if self.first else 0)
            handle = k.CreateNamedPipeW(self.name, mode,
                                        PIPE_TYPE_BYTE | PIPE_READMODE_BYTE | PIPE_WAIT | PIPE_REJECT_REMOTE_CLIENTS,
                                        MAX_INSTANCES, 16384, 16384, 0, ctypes.byref(attributes))
            if handle in (None, windows_proc.INVALID_HANDLE_VALUE):
                windows_proc._fail('CreateNamedPipe '+self.name)
            self.first = False
            return handle
        finally:
            windows_proc._k32().LocalFree(descriptor)

    def accept(self):
        """Block until a client connects; (stream, client pid)."""
        handle = self._create()
        try:
            overlapped = _winapi.ConnectNamedPipe(handle, overlapped=True)
            try:
                _winapi.WaitForMultipleObjects([overlapped.event], False, _winapi.INFINITE)
            finally:
                _, error = overlapped.GetOverlappedResult(True)
            if error not in (0, ERROR_PIPE_CONNECTED):
                raise OSError(error, 'ConnectNamedPipe failed')
            return PipeStream(handle), windows_proc.pipe_client_pid(handle)
        except BaseException:
            _winapi.CloseHandle(handle)
            raise
