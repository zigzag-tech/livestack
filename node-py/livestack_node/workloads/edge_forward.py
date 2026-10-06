"""Stateless forwarding hop for immutable object transfers across a lossy region.

Run `python -m livestack_node.workloads.edge_forward --config /path/config.json`.

It forwards `GET|PUT|HEAD /v1/workloads/objects/<digest>`, `PUT /v1/workloads/upload-grants/<id>/objects/<digest>` and the fixed GitHub
bootstrap/worker-control routes to ONE configured upstream (the workload
authority). Control calls are edge-key gated and capped at 64 KiB; the key is
never forwarded. The relay stores no object bytes, and the authority still
authenticates each request. Traffic is bounded by a monthly byte budget;
exhaustion answers `budget_exhausted` and is logged, never a 404 or empty body.
"""
import argparse
import hmac
import http.client
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sqlite3
import threading
import time
from urllib.parse import urlparse

from .block_codec import HEADER
from .network import BoundedRequests

OBJECT = re.compile(r'/v1/workloads/objects/[0-9a-f]{64}')
# One-use upload grant (upload_grants.py): the holder PUTs the exact object with the opaque capability
# as bearer. Only PUT is forwarded; the authority validates the capability, so the relay stays stateless.
GRANT_OBJECT = re.compile(r'/v1/workloads/upload-grants/[A-Za-z0-9_-]{1,128}/objects/[0-9a-f]{64}')
REMOTE_CONTROL = re.compile(
    r'/v1/workloads/(?:github/bootstrap|worker/(?:status|report|claim|heartbeat|verify-compilation|complete))')
STATUS = '/v1/edge/status'
BUFFER = 256*1024
CONTROL_MAX_BYTES = 64*1024
GRANT_MAX_BYTES = 512*1024*1024
REQUEST_HEADERS = ('authorization', 'range', 'content-type', 'content-length', HEADER.lower())
RESPONSE_HEADERS = ('content-type', 'content-length', 'content-range', 'accept-ranges', HEADER.lower())
KEEP_MONTHS = 3


class Budget:
    """Bytes forwarded per UTC month, both directions. Old months are dropped."""

    def __init__(self, path, cap_bytes, clock=time.time):
        if cap_bytes <= 0:
            raise ValueError('budget must be positive')
        self.cap, self.clock, self._lock, self._pending = cap_bytes, clock, threading.Lock(), 0
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._db.execute('CREATE TABLE IF NOT EXISTS budget(month TEXT PRIMARY KEY, bytes INTEGER NOT NULL)')

    def month(self):
        return time.strftime('%Y-%m', time.gmtime(self.clock()))

    def used(self):
        with self._lock:
            return self._used()+self._pending

    def _used(self):
        row = self._db.execute('SELECT bytes FROM budget WHERE month=?', (self.month(),)).fetchone()
        return row[0] if row else 0

    def admit(self, declared=0):
        """Reserve `declared` bytes for an in-flight request; False when over budget."""
        with self._lock:
            if self._used()+self._pending+declared >= self.cap:
                return False
            self._pending += declared
            return True

    def settle(self, declared, actual):
        with self._lock:
            self._pending -= declared
            self._db.execute('INSERT INTO budget VALUES(?,?) ON CONFLICT(month) DO UPDATE SET bytes=bytes+?',
                             (self.month(), actual, actual))
            self._db.execute('DELETE FROM budget WHERE month NOT IN '
                             '(SELECT month FROM budget ORDER BY month DESC LIMIT ?)', (KEEP_MONTHS,))

    def exhausted(self):
        return not self.admit(0)


class EdgeForwarder(BoundedRequests, ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    max_connections = 4

    def __init__(self, address, upstream, budget, admin_token, edge_key, *, timeout=60, max_connections=None):
        if len(admin_token) < 32 or len(edge_key) < 32 or admin_token == edge_key:
            raise ValueError('admin token and edge key must be distinct and strong')
        parsed = urlparse(upstream)
        if parsed.scheme != 'http' or not parsed.hostname or not parsed.port:
            raise ValueError('upstream must be http://host:port')
        if max_connections is not None:
            if isinstance(max_connections, bool) or not isinstance(max_connections, int) or not 1 <= max_connections <= 32:
                raise ValueError('max_connections must be 1..32')
            self.max_connections = max_connections
        self.configure_connections()
        self.upstream, self.budget, self.admin_token, self.edge_key, self.timeout = (parsed.hostname, parsed.port), budget, admin_token, edge_key, timeout
        self._reported_exhausted = False
        super().__init__(address, Handler)

    def report_budget(self, exhausted):
        """Log each transition once: a steady refusal must not flood the log."""
        if exhausted != self._reported_exhausted:
            self._reported_exhausted = exhausted
            (logging.error if exhausted else logging.warning)(
                'edge relay budget %s (used %d of %d bytes)', 'EXHAUSTED' if exhausted else 'available again',
                self.budget.used(), self.budget.cap)


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(self.server.timeout)

    def log_message(self, fmt, *args):
        logging.info('edge %s %s', self.command, urlparse(self.path).path)

    def reply(self, status, value, headers=()):
        raw = json.dumps(value).encode()
        self.send_response(status)
        for key, val in (('Content-Type', 'application/json'), ('Content-Length', str(len(raw))),
                         ('Cache-Control', 'no-store'), ('Connection', 'close'), *headers):
            self.send_header(key, val)
        self.end_headers()
        self.close_connection = True
        if self.command != 'HEAD':
            self.wfile.write(raw)

    def do_GET(self):
        self.dispatch()

    do_PUT = do_HEAD = do_POST = do_DELETE = do_PATCH = do_OPTIONS = do_GET

    def dispatch(self):
        path = urlparse(self.path).path
        if path == STATUS and self.command == 'GET':
            return self.status()
        if REMOTE_CONTROL.fullmatch(path):
            if self.command != 'POST':
                return self.reply(405, {'error': 'unsupported control operation'})
            return self.forward(path, control=True)
        if GRANT_OBJECT.fullmatch(path):
            if self.command != 'PUT':
                return self.reply(405, {'error': 'unsupported upload operation'})
            return self.forward(path, grant=True)
        if not OBJECT.fullmatch(path):
            return self.reply(404, {'error': 'route not found'})
        if self.command not in ('GET', 'PUT', 'HEAD'):
            return self.reply(405, {'error': 'unsupported object operation'})
        self.forward(path)

    def status(self):
        budget = self.server.budget
        state = 'budget_exhausted' if budget.exhausted() else 'ok'
        body = {'state': state}
        header = self.headers.get('Authorization', '')
        if hmac.compare_digest(header.encode(), ('Bearer '+self.server.admin_token).encode()):
            body.update(month=budget.month(), used_bytes=budget.used(), cap_bytes=budget.cap)
        self.reply(200, body)

    def forward(self, path, *, control=False, grant=False):
        server, declared = self.server, 0
        # Before anything is read or forwarded: the endpoint is public, and an
        # unauthenticated PUT body would otherwise be streamed to the authority
        # (spending the byte budget) before the authority could refuse it.
        # A grant PUT carries its own one-use capability and comes from a collaborator who holds no
        # edge key; it is instead bounded per request (GRANT_MAX_BYTES) and by the monthly budget.
        bearer = self.headers.get('Authorization', '').startswith('Bearer ')
        if grant and not bearer:
            return self.reply(401, {'error': 'upload capability required'})
        if not grant and not hmac.compare_digest(self.headers.get('X-Edge-Key', '').encode(), server.edge_key.encode()):
            return self.reply(401, {'error': 'edge key required'})
        if self.headers.get('Transfer-Encoding'):
            return self.reply(400, {'error': 'transfer encoding is not supported'})
        if self.command == 'PUT' or control:
            try:
                declared = int(self.headers.get('Content-Length', '-1'))
            except ValueError:
                declared = -1
            if declared < 0:
                return self.reply(400, {'error': 'invalid content length'})
            if grant and declared > GRANT_MAX_BYTES:
                return self.reply(413, {'error': 'upload grant byte limit exceeded'})
            if control and declared > CONTROL_MAX_BYTES:
                return self.reply(413, {'error': 'control request byte limit exceeded'})
        if not server.budget.admit(declared):
            server.report_budget(True)
            return self.reply(503, {'error': 'budget_exhausted'}, [('X-Edge-Reason', 'budget_exhausted')])
        server.report_budget(False)
        moved, connection = 0, None
        try:
            connection = http.client.HTTPConnection(*server.upstream, timeout=server.timeout)
            connection.putrequest(self.command, path, skip_host=True, skip_accept_encoding=True)
            connection.putheader('Host', '%s:%d' % server.upstream)
            for key, value in self.headers.items():
                if key.lower() in REQUEST_HEADERS or key.lower().startswith('x-workload-'):
                    connection.putheader(key, value)
            connection.endheaders()
            left = declared
            while left:
                part = self.rfile.read(min(BUFFER, left))
                if not part:
                    raise OSError('client ended upload early')
                connection.send(part)
                left -= len(part)
                moved += len(part)
            response = connection.getresponse()
        except OSError as error:
            logging.warning('edge upstream unavailable for %s: %s', self.command, error)
            server.budget.settle(declared, moved)
            if connection:
                connection.close()
            return self.reply(502, {'error': 'upstream unavailable'})
        try:
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in RESPONSE_HEADERS:
                    self.send_header(key, value)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.close_connection = True
            while self.command != 'HEAD':
                part = response.read(BUFFER)
                if not part:
                    break
                self.wfile.write(part)
                moved += len(part)
        except OSError as error:
            # Truncation is visible to the caller (short Content-Length); its
            # resumable download continues from the bytes it saved.
            logging.warning('edge transfer interrupted after %d bytes: %s', moved, error)
        finally:
            connection.close()
            server.budget.settle(declared, moved)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--config', required=True)
    config = json.loads(Path(parser.parse_args().config).read_text())
    root = Path(config['state_dir']).expanduser()
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    logging.basicConfig(level=logging.INFO)
    server = EdgeForwarder((config.get('bind', '127.0.0.1'), config.get('port', 8803)), config['upstream'],
                           Budget(root/'budget.sqlite', config.get('budget_bytes', 600*10**9)),
                           config['admin_token'], config['edge_key'], timeout=config.get('timeout', 60),
                           max_connections=config.get('max_connections'))
    logging.info('edge relay on %s -> %s', server.server_address, config['upstream'])
    try:
        server.serve_forever(poll_interval=1)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
