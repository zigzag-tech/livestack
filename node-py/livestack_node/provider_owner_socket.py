"""Local provider operator authority from Unix peer UID and private custody."""
import json
import os
import socket
import socketserver
import stat
import struct
from pathlib import Path
from threading import BoundedSemaphore, Thread
from uuid import uuid4

from .provider_fence import FenceRefused


class ProviderOwnerSocket:
    def __init__(self, path, owner_config, source_identity, shutdown, synchronize):
        config = os.stat(owner_config, follow_symlinks=False)
        if not stat.S_ISREG(config.st_mode) or config.st_mode & 0o077 or config.st_uid != os.getuid():
            raise PermissionError('owned_private_provider_configuration_required')
        directory = os.stat(Path(path).parent, follow_symlinks=False)
        if not stat.S_ISDIR(directory.st_mode) or directory.st_mode & 0o077 or directory.st_uid != config.st_uid:
            raise PermissionError('owned_private_control_directory_required')
        if not hasattr(socket, 'SO_PEERCRED'):
            raise RuntimeError('unix_peer_credentials_unsupported')
        self.path, self.uid = str(path), config.st_uid
        self.instance_id = uuid4().hex
        encoded_identity = json.dumps(source_identity, separators=(',', ':'))
        if len(encoded_identity.encode()) > 4096:
            raise ValueError('provider_source_identity_bound')
        self.source_identity = json.loads(encoded_identity)
        self.shutdown, self.synchronize = shutdown, synchronize
        self._credential = object()
        self._server = None
        self.fence = None

    def authorize(self, credential, action):
        if credential is not self._credential or action not in ('hold', 'status', 'release', 'shutdown', 'startup'):
            raise PermissionError('provider_operator_authority_required')

    def dispatch(self, uid, request):
        if uid != self.uid:
            raise PermissionError('provider_operator_peer_uid_refused')
        if not isinstance(request, dict) or set(request) - {'operation', 'holder', 'epoch', 'expectedSource'}:
            raise FenceRefused('invalid_owner_control_request')
        operation = request.get('operation')
        if operation != 'status' and request.get('expectedSource') != self.source_identity:
            raise FenceRefused('provider_source_cas_mismatch')
        if operation == 'hold':
            result = self.fence.hold(self._credential, request.get('holder'))
        elif operation == 'status':
            result = self.fence.status(self._credential)
        elif operation == 'release':
            result = self.fence.release(self._credential, request.get('holder'), request.get('epoch'))
        elif operation == 'shutdown':
            self.fence.shutdown_owned(self._credential, request.get('holder'), request.get('epoch'),
                                      self.shutdown, self.synchronize)
            result = self.fence.status(self._credential)
        else:
            raise FenceRefused('unknown_owner_control_operation')
        return {**result, 'sourceIdentity': self.source_identity, 'serverInstanceId': self.instance_id, 'serverProcessId': os.getpid()}

    def start(self, fence):
        if self._server is not None:
            raise RuntimeError('owner_control_already_started')
        self.fence = fence
        owner = self
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            slots = BoundedSemaphore(8)
            def process_request(self, request, client_address):
                if not self.slots.acquire(blocking=False):
                    request.close()
                    return
                try:
                    super().process_request(request, client_address)
                except BaseException:
                    self.slots.release()
                    raise
            def process_request_thread(self, request, client_address):
                try:
                    super().process_request_thread(request, client_address)
                finally:
                    self.slots.release()
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(20)
                try:
                    _, uid, _ = struct.unpack('3i', self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    payload = self.rfile.readline(8193)
                    if not payload:
                        return
                    if len(payload) > 8192 or not payload.endswith(b'\n'):
                        raise FenceRefused('owner_control_byte_bound')
                    result = owner.dispatch(uid, json.loads(payload))
                    response = {'ok': True, 'result': result}
                except Exception as exc:
                    response = {'ok': False, 'error': type(exc).__name__}
                self.wfile.write(json.dumps(response, separators=(',', ':')).encode() + b'\n')
        # Exclusive bind: never replace a pre-existing controller or socket.
        server = Server(self.path, Handler)
        os.chmod(self.path, 0o600)
        self._inode = os.stat(self.path).st_ino
        self._server = server
        Thread(target=server.serve_forever, daemon=True).start()
        return self

    def close(self):
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        if os.lstat(self.path).st_ino == self._inode:
            os.unlink(self.path)
        self._server = None
