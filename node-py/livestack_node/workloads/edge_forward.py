"""Stateless forwarding hop for immutable object transfers across a lossy region.

Run `python -m livestack_node.workloads.edge_forward --config /path/config.json`.

It forwards `GET|PUT|HEAD /v1/workloads/objects/<digest>`, `PUT /v1/workloads/upload-grants/<id>/objects/<digest>` and the fixed GitHub
bootstrap/worker-control routes to ONE configured upstream (the workload
authority). Control calls are edge-key gated and capped at 64 KiB; the key is
never forwarded. The relay stores no object bytes, and the authority still
authenticates each request. Traffic is bounded by a monthly byte budget;
exhaustion answers `budget_exhausted` and is logged, never a 404 or empty body.

Optionally (`stage_dir` in the config; see `Stager`) it stages whole objects, bounded, to beat slow-start on a
lossy long-RTT upstream; with `stage_dir` unset it stores no bytes at all.
"""
import argparse
import hashlib
import hmac
import http.client
import json
import logging
import os
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


class Stager:
    """Optional bounded staging of whole immutable objects on the relay (off unless `stage_dir` is set).

    Why: a short ranged GET over a lossy long-RTT upstream never leaves TCP slow start, so a 54 MB
    object fetched in 4 MB blocks crawls at a fraction of what ONE warm connection moves. The relay
    pulls the whole object once, verifies sha256 == the digest in the URL, and serves every range from
    that copy. Safety: the authority is still asked (a 1-byte probe with the caller's own headers) before
    every response, so staging never lets a caller read what the authority would refuse. Any staging
    failure is logged LOUDLY and the request falls back to plain pass-through.

    Bounds (all actively enforced, `sweep()` runs on every admission and on a timer): per-object bytes,
    total bytes, object count, age. Leftover files from a crash are removed at start.
    """

    def __init__(self, directory, *, max_object_bytes=512*2**20, max_bytes=2*2**30, max_count=16,
                 max_age_seconds=900, wait_seconds=900, failure_memory_seconds=60, clock=time.time):
        for name, value in (('max_object_bytes', max_object_bytes), ('max_bytes', max_bytes), ('max_count', max_count),
                            ('max_age_seconds', max_age_seconds), ('wait_seconds', wait_seconds)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError('stage %s must be positive' % name)
        if max_object_bytes > max_bytes:
            raise ValueError('stage max_object_bytes exceeds max_bytes')
        self.dir = Path(directory).expanduser()
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        for leftover in self.dir.iterdir():  # staged copies do not survive a restart: bounded by construction
            leftover.unlink()
        self.max_object, self.max_bytes, self.max_count = max_object_bytes, max_bytes, max_count
        self.max_age, self.wait, self.memory, self.clock = max_age_seconds, wait_seconds, failure_memory_seconds, clock
        self._lock, self._have, self._inflight, self._failed = threading.Lock(), {}, {}, {}

    def sweep(self):
        with self._lock:
            self._sweep()

    def _sweep(self, room=0):
        now = self.clock()
        for digest in [d for d, (_s, at) in self._have.items() if now-at > self.max_age]:
            self._drop(digest, 'expired')
        for digest in [d for d, at in self._failed.items() if now-at > self.memory][:256]:
            del self._failed[digest]
        while self._have and (len(self._have) >= self.max_count or sum(s for s, _ in self._have.values())+room > self.max_bytes):
            self._drop(min(self._have, key=lambda d: self._have[d][1]), 'evicted')

    def _drop(self, digest, why):
        self._have.pop(digest, None)
        try:
            (self.dir/digest).unlink()
        except FileNotFoundError:
            pass
        logging.info('edge stage %s digest=%s', why, digest[:12])

    def path(self, digest):
        """The staged file for this digest (touched), or None."""
        with self._lock:
            self._sweep()
            if digest not in self._have:
                return None
            size, _ = self._have[digest]
            self._have[digest] = (size, self.clock())
            return self.dir/digest

    def acquire(self, digest, size):
        """('have', None) | ('lead', event) | ('wait', event) | ('refuse', reason)."""
        with self._lock:
            self._sweep(room=size)
            if digest in self._have:
                return 'have', None
            if digest in self._inflight:
                return 'wait', self._inflight[digest]
            if size > self.max_object:
                return 'refuse', 'oversize'
            if digest in self._failed:
                return 'refuse', 'recent_failure'
            self._inflight[digest] = threading.Event()
            return 'lead', self._inflight[digest]

    def finish(self, digest, event, size, ok):
        with self._lock:
            self._inflight.pop(digest, None)
            if ok:
                self._have[digest] = (size, self.clock())
            else:
                self._failed[digest] = self.clock()
        event.set()

    def partial(self, digest):
        return self.dir/(digest+'.part')


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
                 grant_limits=None, early_reject_seconds=1.5, stager=None):
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
        self.stager = stager
        super().__init__(address, Handler)
        if stager:
            threading.Thread(target=self._sweep_stage, daemon=True).start()

    def _sweep_stage(self):
        while True:  # the age bound must hold with no traffic too
            time.sleep(min(60, self.stager.max_age))
            try:
                self.stager.sweep()
            except OSError as error:
                logging.error('edge stage sweep failed: %s', error)

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

    def upstream_headers(self, *, ranged):
        """Caller headers the authority needs to authorize (never the edge key); optionally the range."""
        keep = ('authorization', 'range') if ranged else ('authorization',)
        return {k: v for k, v in self.headers.items()
                if k.lower() in keep or k.lower().startswith('x-workload-')}

    def serve_staged(self, path):
        """Serve a ranged GET from a staged copy. Returns bytes sent, or None to fall back to pass-through
        (nothing has been written to the caller in that case)."""
        server, stager = self.server, self.server.stager
        digest = path.rsplit('/', 1)[1]
        match = re.fullmatch(r'bytes=([0-9]{1,15})-([0-9]{1,15})', self.headers.get('Range', ''))
        if not match:
            return None
        first, last = int(match.group(1)), int(match.group(2))
        try:  # the authority authorizes every request: a 1-byte probe with the caller's own headers
            connection = http.client.HTTPConnection(*server.upstream, timeout=server.timeout)
            connection.request('GET', path, headers={**self.upstream_headers(ranged=False), 'Range': 'bytes=0-0'})
            probe = connection.getresponse()
            probe.read(2)
            connection.close()
            size = re.fullmatch(r'bytes 0-0/([0-9]{1,15})', probe.getheader('Content-Range', ''))
            if probe.status != 206 or not size:
                return None  # refusal or odd answer: the plain path relays the authority's own response
            size = int(size.group(1))
        except OSError as error:
            logging.warning('edge stage probe failed for %s: %s', digest[:12], error)
            return None
        if first > last or first >= size:
            return None
        last = min(last, size-1)  # like the authority, clamp a range that runs past the end
        state, extra = stager.acquire(digest, size)
        if state == 'refuse':
            if extra == 'oversize':
                logging.info('edge stage skipped digest=%s: %d bytes exceeds the per-object bound', digest[:12], size)
            else:
                logging.warning('edge stage skipped digest=%s: %s', digest[:12], extra)
            return None
        if state == 'lead':
            self.stage_object(path, digest, size, extra)
        elif state == 'wait':
            extra.wait(stager.wait)
        staged = stager.path(digest)
        if staged is None:
            logging.warning('edge stage unavailable for digest=%s: passing through', digest[:12])
            return None
        try:
            handle = staged.open('rb')
        except OSError as error:
            logging.error('edge stage copy unreadable digest=%s: %s', digest[:12], error)
            return None
        with handle:
            length = last-first+1
            self.send_response(206)
            for key, value in (('Content-Type', 'application/octet-stream'), ('Content-Length', str(length)),
                               ('Content-Range', 'bytes %d-%d/%d' % (first, last, size)), ('Accept-Ranges', 'bytes'),
                               ('Connection', 'close')):
                self.send_header(key, value)
            self.end_headers()
            self.close_connection = True
            handle.seek(first)
            left = length
            try:
                while left:
                    part = handle.read(min(BUFFER, left))
                    if not part:
                        break
                    self.wfile.write(part)
                    left -= len(part)
            except OSError as error:
                logging.warning('edge staged transfer interrupted: %s', error)
        return length-left

    def stage_object(self, path, digest, size, event):
        """Pull the whole object over ONE connection, verify its digest, publish it atomically.
        Never raises: on any failure the stage is abandoned loudly and the caller passes through."""
        server, stager = self.server, self.server.stager
        part, ok, moved = stager.partial(digest), False, 0
        started = time.monotonic()
        try:
            connection = http.client.HTTPConnection(*server.upstream, timeout=server.timeout)
            try:
                connection.request('GET', path, headers=self.upstream_headers(ranged=False))
                response = connection.getresponse()
                if response.status != 200 or int(response.getheader('Content-Length', '-1')) != size:
                    raise OSError('upstream answered %d for the full object' % response.status)
                hasher = hashlib.sha256()
                with part.open('wb') as out:
                    while True:
                        block = response.read(BUFFER)
                        if not block:
                            break
                        moved += len(block)
                        if moved > size or time.monotonic()-started > stager.wait:
                            raise OSError('upstream sent more than declared or took too long')
                        hasher.update(block)
                        out.write(block)
            finally:
                connection.close()
            if moved != size:
                raise OSError('short object: %d of %d bytes' % (moved, size))
            if hasher.hexdigest() != digest:
                raise OSError('digest mismatch: upstream bytes are not the named object')
            os.replace(part, stager.dir/digest)
            ok = True
            logging.info('edge stage ok digest=%s bytes=%d seconds=%.1f', digest[:12], size, time.monotonic()-started)
        except (OSError, ValueError) as error:
            logging.error('edge stage FAILED digest=%s after %d bytes: %s; falling back to pass-through',
                          digest[:12], moved, error)
        finally:
            if not ok:
                try:
                    part.unlink()
                except FileNotFoundError:
                    pass
            server.budget.settle(0, moved)  # staging bytes crossed the authority leg: they count
            stager.finish(digest, event, size, ok)

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
        if server.stager and self.command == 'GET' and not control and not path.endswith('/upload'):
            served = self.serve_staged(path)
            if served is not None:
                server.budget.settle(declared, served)
                server.report_thresholds()
                return
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
                           max_connections=config.get('max_connections'),
                           stager=Stager(config['stage_dir'], **config.get('stage', {})) if config.get('stage_dir') else None)
    logging.info('edge relay on %s -> %s', server.server_address, config['upstream'])
    try:
        server.serve_forever(poll_interval=1)
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
