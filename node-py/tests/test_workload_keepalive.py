"""Worker control traffic over kept-alive connections, against a real
authority over real sockets (openspec/changes/worker-control-keepalive).

The incident this guards: a Lima guest whose user-mode NAT admitted 10
in-flight handshakes; once those were full, every renewal that needed a NEW
TCP connect timed out and the lease expired with the authority healthy."""
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import logging
import socket
from threading import Lock, Thread
import time

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.lease import LeaseKeeper
from livestack_node.workloads.store import WorkloadStore


class CountingServer(WorkloadServer):
    """The real authority, counting and holding every accepted connection."""

    def process_request(self, request, client_address):
        with self.accepted_lock:
            self.accepted.append(request)
        super().process_request(request, client_address)


def start(tmp_path, cls=CountingServer):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'native.v1'})
    cls.accepted, cls.accepted_lock = [], Lock()
    server = cls(('127.0.0.1', 0), store, [
        Principal('alice', 'a'*32, 'caller', ('native.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='w1', host='host1')])
    data = b'input fixture'
    digest = hashlib.sha256(data).hexdigest()
    server.blobs.put('alice', digest, len(data), BytesIO(data))
    Thread(target=server.serve_forever, daemon=True).start()
    return server, digest


@pytest.fixture
def authority(tmp_path):
    server, digest = start(tmp_path)
    url = f'http://127.0.0.1:{server.server_port}'
    caller = WorkloadClient(url, 'a'*32)
    caller.submit(dict(version=1, key='one', handler='native.v1', input_digest=digest,
                       need={'cpu': .1, 'memory_bytes': 128*1024**2, 'disk_bytes': 64*1024**2}))
    worker = WorkloadClient(url, 'w'*32, timeout=1)
    capacity = {'cpu': 1, 'memory_bytes': 128*1024**2, 'disk_bytes': 64*1024**3}
    worker.request('worker/report', dict(boot='boot', report=dict(
        capacity=capacity, available=capacity, labels={}, handlers=['native.v1'], ready=True)))
    assignment = worker.request('worker/claim', {'boot': 'boot'})['assignment']
    assert assignment is not None
    yield server, worker, assignment
    server.shutdown()
    server.server_close()


def accepted_during(server, call):
    before = len(server.accepted)
    call()
    return len(server.accepted)-before


def test_one_connection_carries_many_renewals(authority, tmp_path):
    server, worker, assignment = authority
    lease = None

    def renew_many():
        nonlocal lease
        lease = LeaseKeeper(worker, assignment, tmp_path/'lease', interval=60).start()
        for _ in range(20):
            lease.renew()
    try:
        # The initial grant opens the lease connection; 20 renewals reuse it.
        assert accepted_during(server, renew_many) == 1
        assert lease.remaining > 0
    finally:
        lease.close()


def test_renewal_reconnects_after_server_drops_connection(authority, tmp_path):
    server, worker, assignment = authority
    lease = LeaseKeeper(worker, assignment, tmp_path/'lease', interval=60).start()
    try:
        lease.renew()
        with server.accepted_lock:
            for sock in server.accepted:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        time.sleep(.1)  # let the handler threads see EOF
        assert accepted_during(server, lease.renew) == 1
        assert accepted_during(server, lease.renew) == 0
        assert not lease.lost.is_set()
    finally:
        lease.close()


def test_renewal_survives_blocked_new_connects(authority, tmp_path, monkeypatch):
    """After the first grant, every NEW connect hangs until its timeout, as
    behind a NAT whose handshake slots are full. Renewals on the already
    established connection must still succeed. A per-request client fails."""
    _server, worker, assignment = authority
    lease = LeaseKeeper(worker, assignment, tmp_path/'lease', interval=60).start()
    real = socket.create_connection

    def hanging(address, timeout=None, *args, **kwargs):
        time.sleep(timeout if timeout is not None else 1)
        raise TimeoutError('connect blocked (simulated exhausted NAT handshake slots)')
    try:
        monkeypatch.setattr(socket, 'create_connection', hanging)
        for _ in range(5):
            lease.renew()
        assert lease.remaining > 0
    finally:
        monkeypatch.setattr(socket, 'create_connection', real)
        lease.close()


def test_error_response_has_content_length_and_unread_body_closes(tmp_path):
    server, _digest = start(tmp_path)
    try:
        with socket.create_connection(('127.0.0.1', server.server_port), timeout=5) as sock:
            body = b'{"boot": "x"}'
            sock.sendall(b'POST /v1/workloads/worker/claim HTTP/1.1\r\nHost: a\r\n'
                         b'Authorization: Bearer nope\r\nContent-Length: %d\r\n\r\n%s' % (len(body), body))
            raw = b''
            while True:
                part = sock.recv(65536)
                if not part:
                    break  # the server closed: the unread body is never parsed
                raw += part
        head, _, payload = raw.partition(b'\r\n\r\n')
        assert head.startswith(b'HTTP/1.1 401')
        assert b'Content-Length: %d' % len(payload) in head
        assert b'Connection: close' in head
        # A fully read request keeps the connection: two requests, one socket.
        with socket.create_connection(('127.0.0.1', server.server_port), timeout=5) as sock:
            reader = sock.makefile('rb')
            for _ in range(2):
                sock.sendall(b'GET /v1/workloads/jobs HTTP/1.1\r\nHost: a\r\nAuthorization: Bearer '
                             + b'a'*32 + b'\r\n\r\n')
                status = reader.readline()
                headers = {}
                while (line := reader.readline()) not in (b'\r\n', b''):
                    key, _, value = line.decode().partition(':')
                    headers[key.strip().lower()] = value.strip()
                assert status.startswith(b'HTTP/1.1 200')
                assert 'connection' not in headers
                assert json.loads(reader.read(int(headers['content-length'])))['jobs'] == []
    finally:
        server.shutdown()
        server.server_close()


def test_bound_follows_worker_principals(tmp_path):
    server, _digest = start(tmp_path)
    try:
        # 32 shared + 2 kept control connections for the one worker principal.
        assert server.connection_bound() == 32 + 2
    finally:
        server.server_close()


def test_idle_kept_connections_cannot_starve_a_new_request(tmp_path, caplog):
    """Kept-alive workers hold every slot; a verifier/object-style request on
    a NEW connection still succeeds, and the evicted worker reconnects."""
    import urllib.request

    class Capped(CountingServer):
        max_connections = 1  # bound = 1 + 2*1 worker = 3
    server, _digest = start(tmp_path, Capped)
    url = f'http://127.0.0.1:{server.server_port}'
    workers = [WorkloadClient(url, 'w'*32) for _ in range(server.connection_bound())]
    try:
        for worker in workers:
            worker.request('worker/report', dict(boot='b', report=dict(
                capacity={'cpu': 1}, available={'cpu': 1}, labels={}, handlers=['native.v1'], ready=True)))
        time.sleep(.1)  # every kept connection is now idle and holds a slot
        with caplog.at_level(logging.INFO):
            request = urllib.request.Request(url+'/v1/workloads/jobs',
                                             headers={'Authorization': 'Bearer '+'a'*32})
            with urllib.request.urlopen(request, timeout=5) as response:
                assert json.load(response)['jobs'] == []
        assert 'workload_idle_connection_evicted_at_bound: max_connections=3' in caplog.text
        assert 'workload_connection_dropped_at_bound' not in caplog.text
        for worker in workers:  # the evicted one reconnects transparently
            assert worker.request('worker/claim', {'boot': 'b'})['assignment'] is None
    finally:
        for worker in workers:
            worker.close()
        server.shutdown()
        server.server_close()


def test_client_against_server_that_closes_every_response():
    """A new worker against an authority (or edge) that answers
    `Connection: close`: each request reconnects, nothing fails."""
    class Closing(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            raw = json.dumps({'ok': True}).encode()
            self.send_response(200)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Closing)
    Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = WorkloadClient(f'http://127.0.0.1:{server.server_port}', 'w'*32)
        for _ in range(3):
            assert client.request('worker/heartbeat', {}) == {'ok': True}
    finally:
        server.shutdown()
        server.server_close()


def test_kept_connection_keeps_urllib_error_surface(tmp_path):
    """HTTP >= 400 still arrives as the authority's status and message; a
    refused connect is still a transient URLError."""
    import urllib.error
    from livestack_node.workloads.lease import transient
    from livestack_node.workloads.model import WorkloadError
    server, _digest = start(tmp_path)
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        stranger = WorkloadClient(url, 'x'*32)
        with pytest.raises(WorkloadError) as refused:
            stranger.list_jobs()
        assert (refused.value.status, str(refused.value)) == (401, 'authentication required')
        assert WorkloadClient(url, 'a'*32).list_jobs() == []
    finally:
        server.shutdown()
        server.server_close()
    with pytest.raises(urllib.error.URLError) as down:
        WorkloadClient(url, 'a'*32).list_jobs()
    assert transient(down.value)
