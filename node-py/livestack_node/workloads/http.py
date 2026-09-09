"""Authenticated HTTP control plane for durable workloads.

No session-bound jobs and no exception-to-local-execution fallback. All routes
use a configured principal; worker identity comes from the credential, not JSON.
"""
from __future__ import annotations

from dataclasses import dataclass
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
from urllib.parse import urlparse

from .model import WorkloadError, encode, name
from .blobs import BlobStore
from .object_routes import route_object
from .network import BoundedRequests


@dataclass(frozen=True)
class Principal:
    id: str
    token: str
    role: str
    handlers: tuple[str, ...] = ()
    worker: str | None = None
    host: str | None = None

    def __post_init__(self):
        name(self.id, "principal")
        if len(self.token) < 32 or self.role not in ('caller', 'worker', 'admin'):
            raise ValueError('principal requires a strong token and a known role')
        if self.role == 'worker':
            name(self.worker, 'worker')
            name(self.host, 'host')
        elif not self.handlers:
            raise ValueError('caller/admin must declare allowed handlers')


class WorkloadServer(BoundedRequests, ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, store, principals, *, blobs=None, artifact_mirror=None):
        if not principals or len(principals) > 128:
            raise ValueError('configure 1..128 workload principals')
        if len({p.token for p in principals}) != len(principals):
            raise ValueError('principal tokens must be unique')
        self.configure_connections()
        self.store = store
        self.blobs = blobs or BlobStore(store, __import__("pathlib").Path(store.path).parent/"objects")
        self.artifact_mirror = artifact_mirror
        self.principals = tuple(principals)
        super().__init__(address, Handler)

    def service_actions(self):
        # Called periodically even with no traffic: dead clients never leave
        # expiry/retention dependent on the next user making a request.
        import time
        now = time.monotonic()
        if now - getattr(self, '_last_sweep', 0) >= 30:
            self.store.sweep()
            self.blobs.prune()
            self._last_sweep = now


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def log_message(self, fmt, *args):
        # Do not log authorization headers, body content or query parameters.
        logging.info('workloads %s %s', self.command, urlparse(self.path).path)

    def principal(self):
        header = self.headers.get('Authorization', '')
        token = header[7:] if header.startswith('Bearer ') else ''
        for principal in self.server.principals:
            if hmac.compare_digest(token.encode(), principal.token.encode()):
                return principal
        raise WorkloadError('authentication required', 401)

    def body(self):
        if self.headers.get('Transfer-Encoding'):
            raise WorkloadError('transfer encoding is not supported')
        try:
            length = int(self.headers.get('Content-Length', '-1'))
        except ValueError:
            raise WorkloadError('invalid content length')
        if length < 0 or length > self.server.store.limits.record_bytes:
            raise WorkloadError('request byte limit exceeded', 413)
        try:
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise WorkloadError('incomplete request')
            result = json.loads(raw)
        except (ValueError, UnicodeError) as exc:
            raise WorkloadError('invalid JSON') from exc
        if not isinstance(result, dict):
            raise WorkloadError('request must be an object')
        return result

    def respond(self, status, value):
        raw = encode(value, 8*1024*1024).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        self.wfile.write(raw)

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')

    def dispatch(self, method):
        try:
            principal = self.principal()
            parts = urlparse(self.path).path.strip('/').split('/')
            if parts[:2] != ['v1', 'workloads']:
                raise WorkloadError('route not found', 404)
            if route_object(self, principal, method, parts[2:]):
                return
            body = self.body() if method == 'POST' else {}
            result = self.route(principal, method, parts[2:], body)
            self.respond(200, result)
        except WorkloadError as exc:
            self.respond(exc.status, {'error': str(exc)})
        except (KeyError, TypeError, ValueError) as exc:
            self.respond(400, {'error': 'invalid request fields'})
        except Exception:
            logging.exception('workload request failed')
            self.respond(503, {'error': 'workload authority unavailable; no execution grant'})

    def route(self, principal, method, parts, body):
        store = self.server.store
        if principal.role in ('caller', 'admin'):
            if parts == ['jobs']:
                if method == 'POST':
                    if body.get('handler') not in principal.handlers:
                        raise WorkloadError('handler is not authorized', 403)
                    with self.server.blobs.open(principal.id, body.get('input_digest')):
                        pass
                    return store.submit(principal.id, body, allowed_handlers=principal.handlers)
                return {'jobs': store.list_jobs(principal.id)}
            if len(parts) == 2 and parts[0] == 'jobs' and method == 'GET':
                return store.get(principal.id, parts[1])
            if len(parts) == 3 and parts[0] == 'jobs' and parts[2] == 'cancel' and method == 'POST':
                return store.cancel(principal.id, parts[1])
        if principal.role == 'worker' and method == 'POST':
            if parts == ['worker', 'report']:
                return store.register(principal.worker, principal.host, body['boot'], body['report'],
                                      cleaned=body.get('cleaned', ()))
            if parts == ['worker', 'claim']:
                return {'assignment': store.claim(principal.worker, body['boot'])}
            if parts == ['worker', 'heartbeat']:
                return store.heartbeat(principal.worker, body['boot'], body['attempt_id'], body['fence'])
            if parts == ['worker', 'complete']:
                return store.complete(principal.worker, body['boot'], body['attempt_id'], body['fence'],
                                      input_digest=body['input_digest'], outcome=body['outcome'], result=body['result'])
        raise WorkloadError('route not permitted for principal', 403)
