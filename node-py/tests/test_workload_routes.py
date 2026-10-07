"""Route failover for object transfer, against a REAL authority.

Every route here is a real HTTP server on a real socket: a fault proxy in front of the
authority that can reset a connection mid-body (an actual TCP RST), stall, answer 5xx, or
throttle. Nothing is mocked; byte counts are read off the wire at each route.
"""
import hashlib
import http.client
import io
import logging
import os
import socket
import struct
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from livestack_node.workloads.blobs import BlobStore
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.edge_forward import Budget, EdgeForwarder
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.route_kinds import HttpRoute
from livestack_node.workloads.routes import Health, RouteDescriptor, RoutePolicy, RouteSet, Trail
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer

TOKEN = 'a'*32
CHUNK = 64*1024


def serve(server):
    Thread(target=server.serve_forever, daemon=True).start()
    return server


class Proxy(ThreadingHTTPServer):
    """A route: forwards to the authority, applying `behave(request) -> fault|None`.

    Faults: ('status', code) | ('stall', seconds) | ('reset_body', n) on PUT |
    ('truncate', n) on GET | ('slow', seconds_per_64k). Every request is logged."""
    daemon_threads = True

    def __init__(self, upstream_port, behave=None):
        self.upstream_port, self.behave, self.log = upstream_port, behave or (lambda request: None), []
        super().__init__(('127.0.0.1', 0), _ProxyHandler)

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.server_port

    def requests(self, method=None):
        return [r for r in self.log if method is None or r['method'] == method]


class _ProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def rst(self):
        self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
        self.connection.close()
        self.close_connection = True

    def handle_any(self):
        length = int(self.headers.get('Content-Length', '0') or 0)
        request = dict(method=self.command, path=self.path, range=self.headers.get('Range'),
                       content_range=self.headers.get('Content-Range'), length=length, sent=0, n=len(self.server.log))
        self.server.log.append(request)
        fault = self.server.behave(request)
        if fault and fault[0] == 'reset_body' and self.command == 'PUT':
            self.rfile.read(min(fault[1], length))
            request['sent'] = 0  # the chunk never reached the authority
            return self.rst()
        body = self.rfile.read(length) if length else b''
        if fault and fault[0] == 'status':
            self.send_response(fault[1]); self.send_header('Content-Length', '0'); self.end_headers()
            return
        if fault and fault[0] == 'stall':
            time.sleep(fault[1])
            return self.rst()
        headers = {k: v for k, v in self.headers.items() if k.lower() != 'x-harmony-block-encoding'}
        upstream = http.client.HTTPConnection('127.0.0.1', self.server.upstream_port, timeout=10)
        try:
            upstream.request(self.command, self.path, body=body or None, headers=headers)
            response = upstream.getresponse()
            payload = response.read()
            request['sent'] = len(body)
            if fault and fault[0] == 'ack_lost':
                self.send_response(502); self.send_header('Content-Length', '0'); self.end_headers()
                return
            self.send_response(response.status)
            for key, value in response.getheaders():
                if key.lower() in ('content-type', 'content-length', 'content-range', 'etag', 'accept-ranges'):
                    self.send_header(key, value)
            self.end_headers()
            if fault and fault[0] == 'truncate' and self.command == 'GET':
                self.wfile.write(payload[:fault[1]])
                request['delivered'] = min(fault[1], len(payload))
                return self.rst()
            if fault and fault[0] == 'slow':
                for i in range(0, len(payload), 65536):
                    time.sleep(fault[1])
                    self.wfile.write(payload[i:i+65536])
            else:
                self.wfile.write(payload)
            request['delivered'] = len(payload)
        finally:
            upstream.close()

    do_GET = do_PUT = do_HEAD = handle_any


@pytest.fixture
def authority(tmp_path):
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'test.v1'})
    server = serve(WorkloadServer(('127.0.0.1', 0), store, [Principal('owner', TOKEN, 'caller', ('test.v1',))]))
    opened = [server]
    yield type('Authority', (), dict(
        port=server.server_port, store=store, root=tmp_path,
        proxy=staticmethod(lambda behave=None: opened.append(serve(Proxy(server.server_port, behave))) or opened[-1])))
    for item in opened:
        item.shutdown(); item.server_close()


def blob(root, size, name='obj.bin'):
    path = root/name
    path.write_bytes(os.urandom(size))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def route(name, url, priority, *, cost='free', timeout=5, chunk=CHUNK, kind='http', **kw):
    return HttpRoute(RouteDescriptor(name, kind, url, cost=cost, priority=priority, options={'chunk_bytes': chunk, 'max_failures': 1}),
                     WorkloadClient(url, TOKEN, timeout=timeout), **kw)


def transfer(authority, routes, policy=None):
    return InputTransfer(WorkloadClient('http://127.0.0.1:%d' % authority.port, TOKEN), routes=RouteSet(routes, policy))


def outcomes(trail):
    return [(e['route'], e['outcome']) for e in trail]


# ---------------------------------------------------------------- uploads
def test_upload_resumes_on_the_alternate_route_at_the_offset_without_duplicate_bytes(authority, caplog):
    source, digest = blob(authority.root, 5*CHUNK+1234)
    # Route A resets the connection mid-body on every chunk after the second.
    a = authority.proxy(lambda r: ('reset_body', 1000) if r['method'] == 'PUT' and r['content_range'] and
                        not r['content_range'].startswith(('bytes 0-', 'bytes %d-' % CHUNK)) else None)
    b = authority.proxy()
    moved = transfer(authority, [route('a', a.url, 0), route('b', b.url, 1)])
    with caplog.at_level(logging.WARNING):
        result = moved.put(source)
    assert result == {'digest': digest, 'size': source.stat().st_size}
    assert outcomes(moved.last_trail) == [('a', 'failed'), ('b', 'ok')]
    # A carried exactly two chunks; B started at the offset the authority held, not at 0.
    carried = [r for r in a.requests('PUT') if r['sent']]
    assert [r['content_range'].split('-')[0] for r in carried] == ['bytes 0', 'bytes %d' % CHUNK]
    first_b = b.requests('PUT')[0]
    assert first_b['content_range'].startswith('bytes %d-' % (2*CHUNK))
    # Wire accounting: every payload byte crossed exactly once, summed over both routes.
    assert sum(r['sent'] for r in a.requests('PUT')+b.requests('PUT')) == source.stat().st_size
    assert authority.store  # the authority committed a verified object:
    with authority.store.transaction() as db:
        assert db.execute("SELECT size FROM blobs WHERE digest=? AND state='ready'", (digest,)).fetchone()[0] == source.stat().st_size
    assert 'route a abandoned' in caplog.text  # loud, with the route named


def test_a_chunk_whose_ack_is_lost_resynchronises_instead_of_resending(authority):
    source, digest = blob(authority.root, 3*CHUNK)
    state = {'dropped': False}

    def drop_first_ack(request):
        # The chunk lands at the authority, but the caller sees a 502: the bytes crossed, the ack did not.
        if request['method'] == 'PUT' and request['content_range'].startswith('bytes 0-') and not state['dropped']:
            state['dropped'] = True
            return ('ack_lost',)
    a = authority.proxy(drop_first_ack)
    moved = transfer(authority, [route('a', a.url, 0)])
    assert moved.put(source)['digest'] == digest
    ranges = [r['content_range'] for r in a.requests('PUT')]
    assert [r.split('-')[0] for r in ranges] == ['bytes 0', 'bytes 0', 'bytes %d' % CHUNK, 'bytes %d' % (2*CHUNK)]
    # The retry of chunk 1 was answered 409 with the authority's offset; the client moved on without resending.


def test_chunk_digest_mismatch_is_refused_and_leaves_no_misaligned_tail(authority):
    blobs = BlobStore(authority.store, authority.root/'direct-blobs')
    data = os.urandom(2000)
    digest = hashlib.sha256(data).hexdigest()
    with pytest.raises(WorkloadError, match='chunk digest'):
        blobs.put_range('owner', digest, 2000, 0, 1000, io.BytesIO(data[:1000]), '0'*64)
    assert blobs.upload_offset('owner', digest) == 0
    assert blobs.put_range('owner', digest, 2000, 0, 1000, io.BytesIO(data[:1000]), hashlib.sha256(data[:1000]).hexdigest())['offset'] == 1000
    with pytest.raises(WorkloadError) as error:
        blobs.put_range('owner', digest, 2000, 0, 1000, io.BytesIO(data[:1000]))
    assert error.value.status == 409 and error.value.offset == 1000
    assert blobs.put_range('owner', digest, 2000, 1000, 1000, io.BytesIO(data[1000:]))['complete'] is True
    assert blobs.completed_size('owner', digest) == 2000 and blobs.completed_size('other', digest) is None


def test_partial_uploads_are_bounded(authority):
    blobs = BlobStore(authority.store, authority.root/'bounded-blobs')
    for i in range(BlobStore.MAX_PARTIALS):
        data = bytes([i])*100
        blobs.put_range('o', hashlib.sha256(data).hexdigest(), 100, 0, 50, io.BytesIO(data[:50]))
    extra = b'z'*100
    with pytest.raises(WorkloadError) as error:
        blobs.put_range('o', hashlib.sha256(extra).hexdigest(), 100, 0, 50, io.BytesIO(extra[:50]))
    assert error.value.status == 429


def test_an_authority_without_resumable_routes_gets_the_single_request_put(authority):
    source, digest = blob(authority.root, 3*CHUNK)
    a = authority.proxy(lambda r: ('status', 404) if r['path'].endswith('/upload') else None)
    moved = transfer(authority, [route('a', a.url, 0)])
    assert moved.put(source)['digest'] == digest
    assert [r['path'].endswith('/upload') for r in a.requests('PUT')] == [False]  # one whole-object PUT, no chunks


def test_a_5xx_route_and_a_stalled_route_fail_over(authority):
    source, digest = blob(authority.root, 2*CHUNK)
    sick = authority.proxy(lambda r: ('status', 503))
    stalled = authority.proxy(lambda r: ('stall', 3))
    good = authority.proxy()
    moved = transfer(authority, [route('sick', sick.url, 0), route('stalled', stalled.url, 1, timeout=0.5),
                                 route('good', good.url, 2)])
    assert moved.put(source)['digest'] == digest
    assert outcomes(moved.last_trail) == [('sick', 'failed'), ('stalled', 'failed'), ('good', 'ok')]
    assert 'TimeoutError' in moved.last_trail[1]['reason'] or 'timed out' in moved.last_trail[1]['reason']


# -------------------------------------------------------------- downloads
def test_download_resumes_on_the_alternate_route_at_the_saved_offset(authority):
    source, digest = blob(authority.root, 3*CHUNK+777)
    moved = transfer(authority, [route('x', 'http://127.0.0.1:%d' % authority.port, 0)])
    moved.put(source)
    a = authority.proxy(lambda r: ('truncate', 10_000) if r['method'] == 'GET' else None)
    b = authority.proxy()
    moved = transfer(authority, [route('a', a.url, 0), route('b', b.url, 1)])
    got = moved.get(digest, authority.root/'out/obj.bin')
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    assert outcomes(moved.last_trail) == [('a', 'failed'), ('b', 'ok')]
    delivered = sum(r.get('delivered', 0) for r in a.requests('GET'))
    assert delivered > 0
    first = b.requests('GET')[0]
    assert first['range'].startswith('bytes=%d-' % delivered)          # resumed, not restarted
    assert delivered + sum(r.get('delivered', 0) for r in b.requests('GET')) == source.stat().st_size  # no duplicate bytes


def test_download_with_every_route_dead_raises_with_the_trail(authority, caplog):
    source, digest = blob(authority.root, CHUNK)
    transfer(authority, [route('x', 'http://127.0.0.1:%d' % authority.port, 0)]).put(source)
    a = authority.proxy(lambda r: ('status', 503))
    b = authority.proxy(lambda r: ('status', 502))
    moved = transfer(authority, [route('a', a.url, 0), route('b', b.url, 1)])
    with caplog.at_level(logging.ERROR), pytest.raises(Exception) as error:
        moved.get(digest, authority.root/'dead/obj.bin')
    assert [e['route'] for e in error.value.route_trail] == ['a', 'b']
    assert 'all routes failed: a failed' in caplog.text and '; b failed' in caplog.text
    assert not (authority.root/'dead/obj.bin').exists()
    assert not list((authority.root/'dead').glob('.download-*'))


# ------------------------------------------------------ cost and budgets
def test_expensive_route_is_not_used_while_a_free_route_works(authority):
    source, digest = blob(authority.root, 2*CHUNK)
    pricey, free = authority.proxy(), authority.proxy()
    moved = transfer(authority, [route('pricey', pricey.url, 0, cost='expensive'), route('free', free.url, 1)])
    moved.put(source)
    assert pricey.log == [] and outcomes(moved.last_trail) == [('free', 'ok')]


def test_expensive_route_is_a_last_resort_and_can_be_forbidden(authority):
    source, digest = blob(authority.root, 2*CHUNK)
    down = authority.proxy(lambda r: ('status', 503))
    pricey = authority.proxy()
    routes = lambda: [route('free', down.url, 0), route('pricey', pricey.url, 1, cost='expensive')]
    moved = transfer(authority, routes())
    moved.put(source)
    assert outcomes(moved.last_trail) == [('free', 'failed'), ('pricey', 'ok')]
    pricey.log.clear()
    forbidden = transfer(authority, routes(), RoutePolicy(expensive='never'))
    with pytest.raises(Exception) as error:
        forbidden.put(source)
    assert pricey.log == [] and ('pricey', 'ineligible') in outcomes(error.value.route_trail)


def test_an_exhausted_edge_budget_is_a_skip_not_a_health_failure(authority):
    budget = Budget(authority.root/'budget.sqlite', 1000)
    budget.settle(0, 1000)
    relay = serve(EdgeForwarder(('127.0.0.1', 0), 'http://127.0.0.1:%d' % authority.port, budget, 'r'*32, 'k'*32))
    try:
        source, digest = blob(authority.root, 2*CHUNK)
        url = 'http://127.0.0.1:%d' % relay.server_port
        direct = authority.proxy()
        moved = transfer(authority, [route('relay', url, 0, cost='metered', kind='edge_relay', edge_key='k'*32),
                                     route('direct', direct.url, 1)])
        moved.put(source)
        assert outcomes(moved.last_trail) == [('relay', 'skipped'), ('direct', 'ok')]
        assert 'budget_exhausted' in moved.last_trail[0]['reason']
        assert moved.routes.health(moved.routes.routes[0], moved.peer).failures == 0
        assert budget.used() == 1000
    finally:
        relay.shutdown(); relay.server_close()


def test_a_saturated_route_is_skipped_to_the_next_one(authority):
    source, digest = blob(authority.root, CHUNK)
    first, second = authority.proxy(), authority.proxy()
    r1 = route('one', first.url, 0)
    for _ in range(r1.descriptor.max_inflight):
        r1.slots.acquire()
    moved = transfer(authority, [r1, route('two', second.url, 1)], RoutePolicy(slot_wait_seconds=0))
    moved.put(source)
    assert outcomes(moved.last_trail) == [('one', 'failed'), ('two', 'ok')] and first.log == []


# ------------------------------------------------- health and breaker (no I/O)
class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_circuit_opens_half_opens_and_closes_with_backoff_and_decay():
    clock = Clock()
    policy = RoutePolicy(failure_threshold=2, open_seconds=10, max_open_seconds=40, stale_seconds=100)
    health = Health(policy, clock)
    assert health.admit() is None
    health.record(False); health.record(False)
    assert 'open' in health.admit()                       # opened after 2 consecutive failures
    clock.now += 11
    assert health.admit() is None                         # half-open: exactly one trial
    assert 'trial already in flight' in health.admit()
    health.record(False)                                  # trial fails: reopen, backoff doubled to 20
    clock.now += 11
    assert health.admit() is not None
    clock.now += 10
    assert health.admit() is None
    health.record(True, 1000, 1.0)                        # trial succeeds: closed, backoff reset
    assert health.state == 'closed' and health.admit() is None and health.backoff == 10
    health.record(False)
    health.record(False)
    clock.now += 11; health.admit(); health.record(False); clock.now += 21; health.admit(); health.record(False)
    assert health.backoff == 40                           # capped at max_open_seconds
    health.state, health.ok = 'closed', 0.1
    clock.now += 101
    assert health.degraded() is False and health.ok == 1.0  # stale evidence is forgotten


def test_equal_priority_routes_are_ordered_by_measured_throughput_and_degraded_ones_demoted():
    clock = Clock()

    class R:
        def __init__(self, name, priority):
            self.descriptor, self.name = RouteDescriptor(name, 'http', 'http://x', priority=priority), name
        unavailable = lambda self: None
    slow, fast, last = R('slow', 0), R('fast', 0), R('last', 1)
    routes = RouteSet([slow, fast, last], clock=clock)
    routes.health(slow, 'p').record(True, 1_000_000, 10)    # 100 kB/s
    routes.health(fast, 'p').record(True, 10_000_000, 1)    # 10 MB/s
    names = lambda: [r.name for r in routes.candidates('get', 10, Trail(), peer='p')]
    assert names() == ['fast', 'slow', 'last']
    for _ in range(6):
        routes.health(fast, 'p').record(False)              # degraded below 0.5 -> behind healthy routes
    assert names()[0] == 'slow'
    assert [r.name for r in routes.candidates('get', 10, Trail(), peer='other')] == ['slow', 'fast', 'last']  # evidence is per peer: none here, config order


def test_descriptors_are_schema_validated():
    ok = RouteDescriptor.from_config(dict(name='r', kind='http', endpoint='http://h', cost='metered',
                                          constraints=dict(directions=['get'], max_bytes=10, regions=['x'])))
    assert ok.permits('put', 5, 'x') == 'direction put not allowed'
    assert ok.permits('get', 50, 'x') == 'object above max_bytes'
    assert ok.permits('get', 5, 'y') == 'region y not allowed'
    assert ok.permits('get', 5, 'x') is None
    for bad in (dict(name='r', kind='http', endpoint='h', cost='cheap'), dict(name='r', kind='http', endpoint='h', extra=1),
                dict(name='r', kind='http'), dict(name='r', kind='http', endpoint='h', constraints=dict(speed=1)),
                dict(name='r', kind='http', endpoint='h', priority=True)):
        with pytest.raises(ValueError):
            RouteDescriptor.from_config(bad)
    with pytest.raises(ValueError):
        RoutePolicy.from_config(dict(expensive='sometimes'))
    with pytest.raises(ValueError, match='unknown route kind'):
        RouteSet.from_config(dict(routes=[dict(name='r', kind='teleport', endpoint='x')]))
