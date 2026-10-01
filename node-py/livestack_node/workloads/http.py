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
import threading
from urllib.parse import urlparse

from .model import WorkloadError, encode, name
from .blobs import BlobStore
from .object_routes import route_object
from .network import BoundedRequests

from ..fleet_auth import AuthError, Principal as FleetPrincipal, resolve_owner


@dataclass(frozen=True)
class Principal:
    id: str
    token: str
    role: str
    handlers: tuple[str, ...] = ()
    worker: str | None = None
    host: str | None = None
    # Owners this caller may name in labels.owner, by prefix (the same word
    # fleet_auth.Principal uses). None: a fixed principal whose jobs are owned
    # by itself; it may not set labels.owner at all.
    delegate_prefix: str | None = None
    # Per-principal cap on concurrently running attempts (J.3).
    max_running: int | None = None
    on_cap: str = "queue"  # "queue" (default) | "refuse"

    def __post_init__(self):
        name(self.id, "principal")
        if len(self.token) < 32 or self.role not in ('caller', 'worker', 'admin'):
            raise ValueError('principal requires a strong token and a known role')
        if self.role == 'worker':
            name(self.worker, 'worker')
            name(self.host, 'host')
        elif not self.handlers:
            raise ValueError('caller/admin must declare allowed handlers')
        if self.delegate_prefix is not None and self.delegate_prefix:
            name(self.delegate_prefix, "delegate_prefix")
        if (isinstance(self.max_running, bool) or self.max_running is not None
                and (not isinstance(self.max_running, int) or self.max_running < 1)):
            raise ValueError('max_running must be a positive integer or None')
        if self.on_cap not in ('queue', 'refuse'):
            raise ValueError('on_cap must be "queue" or "refuse"')


def check_principals(principals):
    """The rules for a principal set, shared by startup and reload."""
    if not principals or len(principals) > 128:
        raise ValueError('configure 1..128 workload principals')
    if len({p.token for p in principals}) != len(principals):
        raise ValueError('principal tokens must be unique')


def binding_changes(old, new):
    """Ids present in both sets whose identity differs (role, worker, host).

    Those are refused on reload: a worker's id/host key its registered state
    and host budgets, and a caller cannot become a worker under the same id
    without orphaning what it owns. Remove the id and add a new one instead."""
    before = {p.id: (p.role, p.worker, p.host) for p in old}
    return sorted(p.id for p in new
                  if p.id in before and before[p.id] != (p.role, p.worker, p.host))


class WorkloadServer(BoundedRequests, ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 32

    def __init__(self, address, store, principals, *, blobs=None, artifact_mirror=None):
        check_principals(principals)
        self.configure_connections()
        self.store = store
        self.blobs = blobs or BlobStore(store, __import__("pathlib").Path(store.path).parent/"objects")
        self.artifact_mirror = artifact_mirror
        self._principals_lock = threading.Lock()
        self.principals = tuple(principals)
        # The store enforces per-principal caps in placement and submission.
        store.bind_principals(self.principals)
        super().__init__(address, Handler)

    def replace_principals(self, principals):
        """Swap the whole principal set atomically, without a restart.

        Requests read `self.principals` once (one reference), so a request
        already authenticated keeps the set it started with and none can see a
        mix of old and new. Raises ValueError, applying nothing, when the new
        set breaks the startup rules or changes an existing id's binding.
        Counters and job state live in the store's database, not in the
        principal, so unchanged principals lose nothing. In-flight attempts of
        a removed principal are not touched: they end through the lease."""
        principals = tuple(principals)
        check_principals(principals)
        with self._principals_lock:
            changed = binding_changes(self.principals, principals)
            if changed:
                raise ValueError('principal_binding_changed: role/worker/host of '
                                 + ', '.join(changed) + ' cannot change; remove and add a new id')
            self.store.bind_principals(principals)  # caps first: new ids are never uncapped
            self.principals = principals

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

    def _authorized_labels(self, principal, body):
        """labels.owner is reserved: it must sit inside the caller's
        delegate_prefix, refused exactly as /fleet/admit refuses an owner
        outside a delegating principal's prefix."""
        tags = body.get('labels', {})
        if not isinstance(tags, dict):
            raise WorkloadError('invalid labels')
        owner = tags.get('owner')
        if owner is None:
            return
        if not isinstance(owner, str):
            return  # model.submission's labels validation refuses it with 400
        if principal.delegate_prefix is None:
            raise WorkloadError(
                f"'{principal.id}' has no delegate_prefix and cannot set labels.owner", 403)
        try:
            resolve_owner(FleetPrincipal(name=principal.id,
                                         delegate_prefix=principal.delegate_prefix), owner)
        except AuthError as error:
            raise WorkloadError(error.detail, error.status) from error

    def route(self, principal, method, parts, body):
        store = self.server.store
        if principal.role in ('caller', 'admin'):
            if parts == ['jobs']:
                if method == 'POST':
                    if body.get('handler') not in principal.handlers:
                        raise WorkloadError('handler is not authorized', 403)
                    self._authorized_labels(principal, body)
                    with self.server.blobs.open(principal.id, body.get('input_digest')):
                        pass
                    inputs = body.get('input_objects', [])
                    if not isinstance(inputs, list) or any(not isinstance(item, dict) for item in inputs):
                        raise WorkloadError('invalid input objects')
                    for item in inputs:
                        with self.server.blobs.open(principal.id, item.get('digest')) as (_, size):
                            if size != item.get('size'):
                                raise WorkloadError('input object size mismatch')
                    return store.submit(principal.id, body, allowed_handlers=principal.handlers)
                return {'jobs': store.list_jobs(principal.id),
                        'principal': store.principal_status(principal.id)}
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
                return store.heartbeat(principal.worker, body['boot'], body['attempt_id'], body['fence'],
                                       progress=body.get('progress'))
            if parts == ['worker', 'verify-compilation']:
                return store.verify_compilation(principal.worker, body['boot'], body['attempt_id'],
                    body['fence'], input_digest=body['input_digest'], compilation_class=body['class'])
            if parts == ['worker', 'complete']:
                return store.complete(principal.worker, body['boot'], body['attempt_id'], body['fence'],
                                      input_digest=body['input_digest'], outcome=body['outcome'], result=body['result'])
        raise WorkloadError('route not permitted for principal', 403)
