"""Object-transfer liveness against a REAL authority behind a throttling HTTP server.

The throttle is a real socket server that can stall mid-body, trickle, or reset (a TCP RST);
the authority behind it is the real WorkloadServer. Windows are scaled down (a one second stall
window) so the same rule that runs in production at 20 s runs here in seconds.
"""
import http.client
import logging
import socket
import struct
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.route_kinds import HttpRoute
from livestack_node.workloads.routes import RouteDescriptor, RouteSet
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer

TOKEN = 'a'*32
KB = 1000


class Throttle(ThreadingHTTPServer):
    """Forwards GET to the authority and applies behave(request) -> None | (mode, ...):
    ('stall', n) send n body bytes then go silent until closed; ('rate', bytes_per_second);
    ('reset', n) send n body bytes then RST; ('status', code)."""
    daemon_threads = True

    def __init__(self, upstream_port, behave):
        self.upstream_port, self.behave, self.log, self.closed = upstream_port, behave, [], threading.Event()
        super().__init__(('127.0.0.1', 0), _Handler)

    @property
    def url(self):
        return 'http://127.0.0.1:%d' % self.server_port


class _Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        request = dict(range=self.headers.get('Range'), n=len(self.server.log))
        self.server.log.append(request)
        fault = self.server.behave(request)
        if fault and fault[0] == 'status':
            self.send_response(fault[1]); self.send_header('Content-Length', '0'); self.end_headers()
            return
        conn = http.client.HTTPConnection('127.0.0.1', self.server.upstream_port, timeout=10)
        try:
            headers = dict(self.headers)
            headers.pop('X-Harmony-Block-Encoding', None)
            conn.request('GET', self.path, headers=headers)
            response = conn.getresponse()
            body = response.read()
            self.send_response(response.status)
            for key, value in response.getheaders():
                self.send_header(key, value)
            self.end_headers()
            self.close_connection = True
            request['length'] = len(body)
            if not fault:
                self.wfile.write(body)
            elif fault[0] == 'rate':
                step = max(1, fault[1]//20)
                for offset in range(0, len(body), step):
                    self.wfile.write(body[offset:offset+step]); self.wfile.flush()
                    time.sleep(step/fault[1])
            elif fault[0] == 'stall':
                self.wfile.write(body[:fault[1]]); self.wfile.flush()
                self.server.closed.wait(15)
            elif fault[0] == 'reset':
                self.wfile.write(body[:fault[1]]); self.wfile.flush()
                self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
                self.connection.close()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            conn.close()


@pytest.fixture
def authority(tmp_path):
    store = WorkloadStore(tmp_path/'authority.sqlite', handlers={'test.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [Principal('owner', TOKEN, 'caller', ('test.v1',))])
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    opened = []

    def upload(size):
        content = bytes((i*7919+i//251) % 256 for i in range(size))
        source = tmp_path/('input-%d' % size); source.write_bytes(content)
        receipt = InputTransfer(WorkloadClient('http://127.0.0.1:%d' % server.server_port, TOKEN)).put(source)
        return content, receipt['digest']

    def throttle(behave):
        proxy = Throttle(server.server_port, behave)
        Thread(target=proxy.serve_forever, daemon=True).start()
        opened.append(proxy)
        return proxy

    yield type('A', (), dict(port=server.server_port, upload=staticmethod(upload), throttle=staticmethod(throttle)))
    for proxy in opened:
        proxy.closed.set(); proxy.shutdown(); proxy.server_close()
    server.shutdown(); thread.join(5); server.server_close()


def route_to(proxy, name='path', **options):
    descriptor = RouteDescriptor(name, 'http', proxy.url, options=options)
    return HttpRoute(descriptor, WorkloadClient(proxy.url, TOKEN, timeout=30))


def transfer_over(*routes, client=None):
    return InputTransfer(client or WorkloadClient('http://127.0.0.1:1', TOKEN), routes=RouteSet(list(routes)))


FAST = dict(stall_seconds=1, ceiling_minimum=1, max_block=128*1024, min_block=64*1024)


def starts(proxy):
    return [int(r['range'].split('=')[1].split('-')[0]) for r in proxy.log if r['range']]


def test_stalled_path_is_abandoned_by_stall_window_and_resumes_at_the_saved_offset(authority, tmp_path):
    content, digest = authority.upload(600*KB)
    proxy = authority.throttle(lambda r: ('stall', 100*KB) if r['n'] == 0 else None)
    transfer = transfer_over(route_to(proxy, **FAST, floor_rate=1000))
    began = time.monotonic()
    transfer.get(digest, tmp_path/'out')
    assert time.monotonic()-began < 8, 'abandoned by the 1 s stall window, not a longer fixed clock'
    assert (tmp_path/'out').read_bytes() == content
    assert starts(proxy)[0] == 100*KB, 'the next request continues at the bytes already saved, not at 0'
    event = transfer.last_trail[0]
    assert event['outcome'] == 'ok' and event['stalls'] == 1, event
    assert event['min_block_bytes'] < FAST['max_block'] or event['min_block_bytes'] == FAST['min_block']


def test_a_slow_path_that_keeps_moving_is_not_bounded_by_a_fixed_clock(authority, tmp_path):
    content, digest = authority.upload(600*KB)
    proxy = authority.throttle(lambda r: ('rate', 150*KB))
    transfer = transfer_over(route_to(proxy, stall_seconds=1, ceiling_minimum=1, floor_rate=20*KB))
    began = time.monotonic()
    transfer.get(digest, tmp_path/'out')
    took = time.monotonic()-began
    assert took > 3, 'the transfer outlived the 1 s stall window and 1 s minimum while moving'
    assert (tmp_path/'out').read_bytes() == content
    assert len(proxy.log) == 1, 'one request carried it: no abandonment while bytes flow'


def test_a_trickle_below_the_floor_rate_fails_with_a_named_duration_cause(authority, tmp_path):
    content, digest = authority.upload(400*KB)
    proxy = authority.throttle(lambda r: ('rate', 40*KB))
    transfer = transfer_over(route_to(proxy, **FAST, floor_rate=100*KB, max_failures=2))
    with pytest.raises(WorkloadError) as caught:
        transfer.get(digest, tmp_path/'out')
    assert 'DurationExceeded' in str(caught.value) or 'duration' in str(caught.value)
    assert not (tmp_path/'out').exists()


def test_a_slow_route_is_asked_for_smaller_blocks_and_the_trail_says_so(authority, tmp_path):
    content, digest = authority.upload(700*KB)
    proxy = authority.throttle(lambda r: ('reset', 100*KB) if r['n'] == 0 else ('rate', 120*KB))
    transfer = transfer_over(route_to(proxy, stall_seconds=5, ceiling_minimum=5, floor_rate=10*KB,
                                      max_block=512*1024, min_block=64*1024, slow_rate=1024*1024))
    transfer.get(digest, tmp_path/'out')
    assert (tmp_path/'out').read_bytes() == content
    lengths = [r['length'] for r in proxy.log[1:]]
    assert max(lengths) < 512*1024, 'a slow route never gets a full-size block: %s' % lengths
    assert transfer.last_trail[0]['block_bytes'] < 512*1024
    assert transfer.last_trail[0]['rate_bps'] < 1024*1024


def test_a_reset_mid_range_costs_one_smaller_block(authority, tmp_path):
    content, digest = authority.upload(500*KB)
    proxy = authority.throttle(lambda r: ('reset', 90*KB) if r['n'] in (0, 1) else None)
    transfer_over(route_to(proxy, stall_seconds=5, max_block=256*1024, min_block=64*1024)).get(digest, tmp_path/'out')
    assert (tmp_path/'out').read_bytes() == content
    ranges = [r['range'] for r in proxy.log]
    length = lambda text: int(text.split('-')[1])-int(text.split('=')[1].split('-')[0])+1
    assert ranges[0] is None, 'the first request is the plain GET'
    assert starts(proxy)[:2] == [90*KB, 180*KB], 'each retry starts at the bytes already saved'
    assert [length(ranges[1]), length(ranges[2])] == [128*1024, 64*1024], 'each reset halved the block'


def test_a_door_failure_is_named_and_the_fallthrough_carries_its_path_class(authority, tmp_path):
    content, digest = authority.upload(100*KB)
    broken = authority.throttle(lambda r: ('status', 503))
    direct = authority.throttle(lambda r: None)
    door = HttpRoute(RouteDescriptor('door', 'edge_relay', broken.url, priority=0), WorkloadClient(broken.url, TOKEN))
    fallback = HttpRoute(RouteDescriptor('authority', 'http', direct.url, priority=1,
                                         options={'degraded_path': 'mesh_derp'}), WorkloadClient(direct.url, TOKEN))
    transfer = transfer_over(door, fallback)
    transfer.get(digest, tmp_path/'out')
    first, last = transfer.last_trail[-2:]
    assert first['route'] == 'door' and first['outcome'] == 'skipped' and 'unreachable' in first['reason']
    assert 'degraded_path' not in first
    assert last['route'] == 'authority' and last['outcome'] == 'ok' and last['degraded_path'] == 'mesh_derp'


def test_single_route_is_reported_for_a_cross_region_worker_with_fewer_than_two_doors(authority, caplog):
    client = WorkloadClient('http://127.0.0.1:1', TOKEN)
    relay = lambda name: HttpRoute(RouteDescriptor(name, 'edge_relay', 'http://127.0.0.1:2'), client, edge_key='k'*32)
    direct = HttpRoute(RouteDescriptor('authority', 'http', 'http://127.0.0.1:1', priority=1), client)
    assert InputTransfer(client).health()['single_route'] is False, 'a same-region worker has no doors to want'
    with caplog.at_level(logging.WARNING):
        one = InputTransfer(client, routes=RouteSet([relay('osaka'), direct]))
    assert one.health() == dict(cross_region=True, doors=['osaka'], single_route=True)
    assert 'single_route' in caplog.text and 'osaka' in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        two = InputTransfer(client, routes=RouteSet([relay('osaka'), relay('ashburn'), direct]))
    assert two.health()['single_route'] is False and 'single_route' not in caplog.text
    assert InputTransfer(client, routes=RouteSet([direct]), cross_region=True).health()['single_route'] is True
