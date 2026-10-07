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
import select
import threading
import time
from urllib.parse import urlparse

from .block_codec import HEADER
from .network import BoundedRequests

OBJECT = re.compile(r'/v1/workloads/objects/[0-9a-f]{64}(?:/upload)?')
# One-use upload grant (upload_grants.py): the holder PUTs the exact object with the opaque capability
# as bearer. Only PUT is forwarded; the authority validates the capability, so the relay stays stateless.
GRANT_OBJECT = re.compile(r'/v1/workloads/upload-grants/[0-9a-f]{32}/objects/[0-9a-f]{64}')
CAPABILITY = re.compile(r'Bearer [A-Za-z0-9._~+/=-]{20,512}')
REMOTE_CONTROL = re.compile(
    r'/v1/workloads/(?:github/bootstrap|worker/(?:status|report|claim|heartbeat|verify-compilation|complete))')
STATUS = '/v1/edge/status'
BUFFER = 256*1024
CONTROL_MAX_BYTES = 64*1024
GRANT_MAX_BYTES = 512*1024*1024
REQUEST_HEADERS = ('authorization', 'range', 'content-range', 'x-chunk-digest', 'content-type', 'content-length', HEADER.lower())
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


class GrantLimits:
    """Admission for the unauthenticated grant route: per-IP and global sliding windows plus a
    concurrent-upload cap. Every request counts, accepted or not, so junk is rate-limited too.
    Returns None to admit or the NAMED refusal."""

    def __init__(self, per_ip=6, global_=30, window=60, concurrent=3, clock=time.monotonic):
        self.per_ip, self.global_, self.window, self.concurrent, self.clock = per_ip, global_, window, concurrent, clock
        self._ip, self._all, self._active, self._lock = {}, [], 0, threading.Lock()

    def _trim(self, hits, now):
        while hits and hits[0] <= now-self.window:
            hits.pop(0)

    def admit(self, ip):
        with self._lock:
            now = self.clock()
            self._trim(self._all, now)
            hits = self._ip.setdefault(ip, [])
            self._trim(hits, now)
            for key in [k for k, v in self._ip.items() if not v and k != ip][:64]:
                del self._ip[key]
            if len(hits) >= self.per_ip:
                return 'rate_limited_ip'
            if len(self._all) >= self.global_:
                return 'rate_limited_global'
            if self._active >= self.concurrent:
                return 'too_many_uploads'
            hits.append(now)
            self._all.append(now)
            self._active += 1
            return None

    def done(self):
        with self._lock:
            self._active -= 1


class EdgeForwarder(BoundedRequests, ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 16
    max_connections = 4

    def __init__(self, address, upstream, budget, admin_token, edge_key, *, timeout=60, max_connections=None,
                 grant_limits=None, early_reject_seconds=1.5):
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
        self.grant_limits, self.early_reject_seconds = grant_limits or GrantLimits(), early_reject_seconds
        self._crossed = set()
        super().__init__(address, Handler)

    def report_thresholds(self):
        """One log line (and one alert signal) per month when usage first crosses 50/80/100%."""
        used, month = self.budget.used(), self.budget.month()
        for percent in (50, 80, 100):
            if used*100 >= self.budget.cap*percent and (month, percent) not in self._crossed:
                self._crossed.add((month, percent))
                logging.error('edge relay budget ALERT: %d%% of the monthly cap crossed (used %d of %d bytes)',
                              percent, used, self.budget.cap)

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
            return self.forward_grant(path)
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

    def client_ip(self):
        """Behind Caddy the peer is loopback and Caddy appends the real address to X-Forwarded-For: the
        LAST entry is the one our own proxy observed (earlier ones are client-supplied)."""
        peer = self.client_address[0]
        if peer in ('127.0.0.1', '::1'):
            forwarded = self.headers.get('X-Forwarded-For', '').split(',')[-1].strip()
            if re.fullmatch(r'[0-9a-fA-F:.]{3,45}', forwarded):
                return forwarded
        return peer

    def grant_log(self, grant_id, outcome, size, ip):
        # Never the Authorization header or the capability: grant id, outcome, size, caller address only.
        logging.info('edge grant-upload grant=%s outcome=%s bytes=%d ip=%s', grant_id, outcome, size, ip)

    def forward_grant(self, path):
        """PUT of a one-use upload grant by a collaborator who holds no edge key. Everything cheap is
        refused BEFORE a body byte is read; the authority's own header-time capability check is then
        awaited (early_reject_seconds) before the body is streamed, so a wrong capability costs the
        relay no upload bytes."""
        server, ip = self.server, self.client_ip()
        grant_id = path.split('/')[4]

        def refuse(status, name, headers=()):
            self.grant_log(grant_id, name, 0, ip)
            return self.reply(status, {'error': name}, headers)
        if not CAPABILITY.fullmatch(self.headers.get('Authorization', '')):
            return refuse(401, 'upload_capability_required')
        if self.headers.get('Transfer-Encoding'):
            return refuse(400, 'transfer_encoding_unsupported')
        try:
            declared = int(self.headers.get('Content-Length', ''))
        except ValueError:
            return refuse(411, 'content_length_required')
        if declared <= 0:
            return refuse(400, 'content_length_invalid')
        if declared > GRANT_MAX_BYTES:
            return refuse(413, 'upload_grant_byte_limit_exceeded')
        limited = server.grant_limits.admit(ip)
        if limited:
            return refuse(429, limited, [('Retry-After', '60')])
        try:
            if not server.budget.admit(declared):
                server.report_budget(True)
                return refuse(503, 'budget_exhausted', [('X-Edge-Reason', 'budget_exhausted')])
            server.report_budget(False)
            moved, connection, early = 0, None, False
            try:
                connection = http.client.HTTPConnection(*server.upstream, timeout=server.timeout)
                connection.putrequest('PUT', path, skip_host=True, skip_accept_encoding=True)
                connection.putheader('Host', '%s:%d' % server.upstream)
                for key, value in self.headers.items():
                    if key.lower() in REQUEST_HEADERS:
                        connection.putheader(key, value)
                connection.endheaders()
                # The authority decides from headers alone (capability, object, size, expiry). A refusal
                # arrives at once; acceptance is silent until the body is read. Wait briefly for a refusal.
                early = bool(select.select([connection.sock], [], [], server.early_reject_seconds)[0])
                if not early:
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
                logging.warning('edge grant upstream/client failure grant=%s: %s', grant_id, error)
                server.budget.settle(declared, moved)
                if connection:
                    connection.close()
                self.grant_log(grant_id, 'upstream_unavailable_or_client_gone', moved, ip)
                return self.reply(502, {'error': 'upstream unavailable'})
            try:
                data = response.read(65536)
                self.send_response(response.status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Connection', 'close')
                self.end_headers()
                self.close_connection = True
                self.wfile.write(data)
            except OSError as error:
                logging.warning('edge grant reply interrupted grant=%s: %s', grant_id, error)
            finally:
                connection.close()
                server.budget.settle(declared, moved)
                server.report_thresholds()
                self.grant_log(grant_id, 'upstream_%d%s' % (response.status, '_early' if early else ''), moved, ip)
        finally:
            server.grant_limits.done()

    def forward(self, path, *, control=False):
        server, declared = self.server, 0
        # Before anything is read or forwarded: the endpoint is public, and an
        # unauthenticated PUT body would otherwise be streamed to the authority
        # (spending the byte budget) before the authority could refuse it.
        if not hmac.compare_digest(self.headers.get('X-Edge-Key', '').encode(), server.edge_key.encode()):
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
            server.report_thresholds()


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
