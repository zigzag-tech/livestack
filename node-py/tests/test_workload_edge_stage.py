"""Edge-relay object staging against a REAL authority behind a slow, cold-start-penalized upstream stub.

The stub models a lossy long-RTT path: the first MiB of every response crawls (slow start), the rest is
fast. Many short ranged requests therefore crawl; one long request does not. That is the property the
staging exists for, so the positive control is that the staged path beats pass-through on the same stub."""
import hashlib
import http.client
import logging
import os
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.edge_forward import Budget, EdgeForwarder, Stager
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer

ADMIN, KEY, OWNER, OTHER = 'r'*32, 'k'*32, 'a'*32, 'b'*32
MIB = 1024*1024
SLOW_BYTES, SLOW_RATE, FAST_RATE = 2*MIB, 1.5*MIB, 200*MIB


def serve(server):
    Thread(target=server.serve_forever, daemon=True).start()


class Lossy(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, authority_port):
        self.authority_port, self.seen, self.corrupt_full, self.slow = authority_port, [], False, True
        super().__init__(('127.0.0.1', 0), LossyHandler)


class LossyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def do_GET(self):
        server = self.server
        headers = {k: v for k, v in self.headers.items() if k.lower() != 'host'}
        server.seen.append((self.path.rsplit('/', 1)[-1][:12], self.headers.get('Range')))
        connection = http.client.HTTPConnection('127.0.0.1', server.authority_port, timeout=30)
        connection.request('GET', self.path, headers=headers)
        response = connection.getresponse()
        body = response.read()
        if server.corrupt_full and self.headers.get('Range') is None and response.status == 200:
            body = bytes([body[0] ^ 1]) + body[1:]
        self.send_response(response.status)
        for key, value in response.getheaders():
            if key.lower() in ('content-type', 'content-length', 'content-range', 'accept-ranges', 'x-harmony-block-encoding'):
                self.send_header(key, value)
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        sent = 0
        while sent < len(body):
            piece = body[sent:sent+65536]
            if server.slow:
                time.sleep(len(piece)/(SLOW_RATE if sent < SLOW_BYTES else FAST_RATE))
            self.wfile.write(piece)
            sent += len(piece)


@pytest.fixture
def rig(tmp_path):
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'test.v1'})
    authority = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', OWNER, 'caller', ('test.v1',), upload_grants=True),
        Principal('other', OTHER, 'caller', ('test.v1',)),
    ])
    serve(authority)
    lossy = Lossy(authority.server_port)
    serve(lossy)
    made = []

    def relay(stage=True, **stage_options):
        stager = Stager(tmp_path/f'stage{len(made)}', **stage_options) if stage else None
        server = EdgeForwarder(('127.0.0.1', 0), f'http://127.0.0.1:{lossy.server_port}',
                               Budget(tmp_path/f'budget{len(made)}.sqlite', 10**10), ADMIN, KEY, stager=stager)
        serve(server)
        made.append(server)
        return server, f'http://127.0.0.1:{server.server_port}'

    def upload(size):
        path = tmp_path/f'o{size}-{len(made)}.bin'
        path.write_bytes(os.urandom(size))
        InputTransfer(WorkloadClient(f'http://127.0.0.1:{authority.server_port}', OWNER)).put(path)
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def fetch(url, digest, name):
        client = WorkloadClient(url, OWNER, edge_key=KEY)
        started = time.monotonic()
        out = InputTransfer(client, relay=client, relay_key=KEY, relay_parallel=1).get(digest, tmp_path/name)
        return time.monotonic()-started, hashlib.sha256(out.read_bytes()).hexdigest()

    yield type('Rig', (), dict(relay=staticmethod(relay), upload=staticmethod(upload), fetch=staticmethod(fetch),
                               lossy=lossy, root=tmp_path))
    for server in made + [lossy, authority]:
        server.shutdown()
        server.server_close()


def ranged(url, digest, token, first, last):
    request = urllib.request.Request(f'{url}/v1/workloads/objects/{digest}', headers={
        'Authorization': 'Bearer '+token, 'X-Edge-Key': KEY, 'Range': f'bytes={first}-{last}'})
    return urllib.request.urlopen(request, timeout=60)


def test_staged_path_beats_pass_through_and_is_byte_identical(rig, caplog):
    caplog.set_level(logging.INFO)
    digest = rig.upload(12*MIB)
    _s, plain_url = rig.relay(stage=False)
    plain_seconds, plain_hash = rig.fetch(plain_url, digest, 'plain')
    server, staged_url = rig.relay()
    staged_seconds, staged_hash = rig.fetch(staged_url, digest, 'staged')
    again_seconds, again_hash = rig.fetch(staged_url, digest, 'again')
    print(f'\n12 MiB through a cold-start-penalized upstream: pass-through {plain_seconds:.1f}s, '
          f'staged first {staged_seconds:.1f}s, staged second {again_seconds:.1f}s')
    assert plain_hash == staged_hash == again_hash == digest
    assert staged_seconds < plain_seconds*0.75   # positive control: the stage must actually win
    assert again_seconds < staged_seconds
    assert (server.stager.dir/digest).stat().st_size == 12*MIB
    assert KEY not in caplog.text and OWNER not in caplog.text


def test_concurrent_requests_for_one_object_pull_it_once(rig):
    digest = rig.upload(6*MIB)
    server, url = rig.relay()
    results = []

    def one(i):
        with ranged(url, digest, OWNER, i*MIB, i*MIB+MIB-1) as response:
            results.append((i, response.status, len(response.read())))
    threads = [Thread(target=one, args=(i,)) for i in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [(i, 206, MIB) for i in range(4)]
    full_pulls = [s for s in rig.lossy.seen if s[1] is None]
    assert len(full_pulls) == 1


def test_a_staged_copy_never_widens_authorization(rig):
    digest = rig.upload(3*MIB)
    server, url = rig.relay()
    ranged(url, digest, OWNER, 0, 99).read()           # stages it
    assert (server.stager.dir/digest).exists()
    with pytest.raises(urllib.error.HTTPError) as error:   # another caller does not own it
        ranged(url, digest, OTHER, 0, 99)
    assert error.value.code in (403, 404)
    with pytest.raises(urllib.error.HTTPError) as error:
        ranged(url, digest, 'z'*32, 0, 99)
    assert error.value.code == 401


def test_oversize_objects_pass_through_and_are_not_stored(rig):
    digest = rig.upload(5*MIB)
    server, url = rig.relay(max_object_bytes=2*MIB, max_bytes=4*MIB)
    seconds, got = rig.fetch(url, digest, 'big')
    assert got == digest
    assert list(server.stager.dir.iterdir()) == []


def test_a_corrupt_stage_is_discarded_loudly_and_the_caller_still_gets_correct_bytes(rig, caplog):
    caplog.set_level(logging.INFO)
    digest = rig.upload(5*MIB)
    server, url = rig.relay()
    rig.lossy.corrupt_full = True
    seconds, got = rig.fetch(url, digest, 'c')
    assert got == digest                                 # pass-through ranges are clean
    assert list(server.stager.dir.iterdir()) == []        # corrupt bytes were never published
    assert 'edge stage FAILED' in caplog.text and 'digest mismatch' in caplog.text
    full_pulls = [s for s in rig.lossy.seen if s[1] is None]
    # the client's own first (rangeless) request plus ONE stage attempt; the failure is remembered, no storm per block
    assert len(full_pulls) == 2


def test_an_unreachable_upstream_for_the_stage_falls_back_without_serving_garbage(rig, caplog):
    digest = rig.upload(3*MIB)
    server, url = rig.relay()
    server.stager.max_object = 10                          # nothing fits
    assert ranged(url, digest, OWNER, 0, 9).read() is not None
    assert list(server.stager.dir.iterdir()) == []


def test_bounds_count_bytes_and_age_are_enforced_actively(tmp_path):
    now = [1000.0]
    stager = Stager(tmp_path/'s', max_object_bytes=100, max_bytes=250, max_count=2, max_age_seconds=60, clock=lambda: now[0])
    for digest, size in (('a'*64, 100), ('b'*64, 100)):
        state, event = stager.acquire(digest, size)
        assert state == 'lead'
        (stager.dir/digest).write_bytes(b'x'*size)
        stager.finish(digest, event, size, True)
        now[0] += 1
    state, event = stager.acquire('c'*64, 100)           # count cap 2: the oldest ('a') is evicted
    assert state == 'lead' and not (stager.dir/('a'*64)).exists() and (stager.dir/('b'*64)).exists()
    (stager.dir/('c'*64)).write_bytes(b'x'*100)
    stager.finish('c'*64, event, 100, True)
    assert stager.acquire('d'*64, 101)[0] == 'refuse'      # per-object bound
    now[0] += 61
    stager.sweep()                                         # age: everything expires with no traffic
    assert list(stager.dir.iterdir()) == []
    state, event = stager.acquire('e'*64, 100)
    stager.finish('e'*64, event, 100, False)
    assert stager.acquire('e'*64, 100) == ('refuse', 'recent_failure')
    now[0] += 61
    assert stager.acquire('e'*64, 100)[0] == 'lead'


def test_leftovers_from_a_crash_are_removed_at_start(tmp_path):
    (tmp_path/'s').mkdir()
    (tmp_path/'s'/'x.part').write_bytes(b'junk')
    Stager(tmp_path/'s')
    assert list((tmp_path/'s').iterdir()) == []
