"""The edge relay in front of a REAL authority.

It forwards bounded release-control and object routes, keeps the authority's
authorization, strips its transport key, refuses over budget by name, and
stores no object bytes. A small fake upstream is used only to inspect headers
that the real authority intentionally ignores."""
import hashlib
import json
import logging
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
import urllib.error
import urllib.request

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.edge_forward import Budget, EdgeForwarder
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer

ADMIN = 'r'*32
KEY = 'k'*32


def serve(server):
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def stop(server, thread):
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


@pytest.fixture
def fleet(tmp_path):
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'test.v1'})
    authority = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', 'a'*32, 'caller', ('test.v1',), upload_grants=True),
        Principal('worker', 'w'*32, 'worker', worker='cn-1', host='cn'),
    ])
    a_thread = serve(authority)
    upstream = f'http://127.0.0.1:{authority.server_port}'

    def relay(cap=10**9, upstream_url=upstream):
        state = tmp_path/'relay'
        state.mkdir(exist_ok=True)
        server = EdgeForwarder(('127.0.0.1', 0), upstream_url, Budget(state/'budget.sqlite', cap), ADMIN, KEY)
        return server, serve(server), f'http://127.0.0.1:{server.server_port}'

    started = []

    def make(**kw):
        made = relay(**kw)
        started.append(made[:2])
        return made

    yield type('Fleet', (), dict(upstream=upstream, make=staticmethod(make), root=tmp_path))
    for server, thread in started:
        stop(server, thread)
    stop(authority, a_thread)


def claim(upstream, digest_owner_client, uploaded, extra_key='one'):
    caller = digest_owner_client
    caller.submit(dict(version=1, key=extra_key, handler='test.v1', input_digest=uploaded['digest'], need={'cpu': 1}))
    worker = WorkloadClient(upstream, 'w'*32)
    worker.request('worker/report', dict(boot='b1', report=dict(
        capacity={'cpu': 2}, available={'cpu': 2}, labels={}, handlers=['test.v1'], ready=True)))
    return worker, worker.request('worker/claim', {'boot': 'b1'})['assignment']


def blob(tmp_path, size, name='in.bin'):
    path = tmp_path/name
    path.write_bytes(os.urandom(size))
    return path, hashlib.sha256(path.read_bytes()).hexdigest()


def test_fetch_and_upload_flow_through_the_relay_and_leave_nothing_behind(fleet):
    server, _t, url = fleet.make()
    caller = WorkloadClient(fleet.upstream, 'a'*32)
    # 9 MiB: more than one 4 MiB range, so the resumable path runs through the hop.
    source, digest = blob(fleet.root, 9*1024*1024)
    uploaded = InputTransfer(WorkloadClient(fleet.upstream, 'a'*32),
                             relay=WorkloadClient(url, 'a'*32), relay_key=KEY).put(source)
    assert uploaded['digest'] == digest
    worker, assignment = claim(fleet.upstream, caller, uploaded)
    got = InputTransfer(worker, relay=WorkloadClient(url, 'w'*32), relay_key=KEY).get(digest, fleet.root/'w/in.bin', assignment=assignment)
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    assert server.budget.used() >= 2*9*1024*1024  # upload plus download both crossed it
    # Stateless: the only file the relay owns is its budget counter.
    assert sorted(p.name for p in (fleet.root/'relay').iterdir()) == ['budget.sqlite']


def test_only_fixed_remote_and_object_routes_are_forwarded(fleet):
    _s, _t, url = fleet.make()
    for path, method in (('/v1/workloads/jobs', 'GET'), ('/v1/workloads/worker/retry', 'POST'),
                         ('/v1/fleet/admit', 'GET')):
        request = urllib.request.Request(url+path, method=method, data=b'{}' if method == 'POST' else None,
                                         headers={'Authorization': 'Bearer '+'a'*32, 'X-Edge-Key': KEY})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 404
    digest = 'f'*64
    request = urllib.request.Request(f'{url}/v1/workloads/objects/{digest}', method='DELETE')
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 405


def test_remote_worker_control_reaches_the_real_authority_with_both_auth_layers(fleet):
    _s, _t, url = fleet.make()
    client = WorkloadClient(url, 'w'*32, edge_key=KEY)
    capacity = {'cpu': 2}
    report = client.request('worker/report', dict(boot='relay-boot', report=dict(
        capacity=capacity, available=capacity, labels={}, handlers=['test.v1'], ready=True)))
    assert report['worker'] == 'cn-1'
    assert client.request('worker/claim', {'boot':'relay-boot'})['assignment'] is None
    client.close()


def test_github_bootstrap_path_is_forwarded_but_authority_still_refuses_without_provider(fleet):
    server, _t, url = fleet.make()
    request = urllib.request.Request(url+'/v1/workloads/github/bootstrap', method='POST', data=b'{}',
        headers={'X-Edge-Key':KEY,'Content-Type':'application/json'})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 404
    assert json.loads(error.value.read()) == {'error':'GitHub remote execution is not configured'}
    assert server.budget.used() > 0


def test_the_authority_still_authorizes(fleet):
    _s, _t, url = fleet.make()
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(urllib.request.Request(
            f'{url}/v1/workloads/objects/{"f"*64}', headers={'X-Edge-Key': KEY}))
    assert error.value.code == 401  # the authority's refusal, passed through
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(urllib.request.Request(
            f'{url}/v1/workloads/objects/{"f"*64}', headers={'X-Edge-Key': KEY, 'Authorization': 'Bearer '+'z'*32}))
    assert error.value.code == 401


def test_a_request_without_the_edge_key_is_refused_before_any_bytes_move(fleet):
    server, _t, url = fleet.make()
    body = os.urandom(300_000)
    digest = hashlib.sha256(body).hexdigest()
    for headers in ({}, {'X-Edge-Key': 'x'*32}, {'X-Edge-Key': KEY[:-1]}):
        request = urllib.request.Request(f'{url}/v1/workloads/objects/{digest}', method='PUT', data=body,
                                         headers={'Authorization': 'Bearer '+'a'*32, **headers})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 401
        assert json.loads(error.value.read()) == {'error': 'edge key required'}
    control = urllib.request.Request(url+'/v1/workloads/worker/claim', method='POST', data=b'{}',
        headers={'Authorization':'Bearer '+'w'*32,'Content-Type':'application/json'})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(control)
    assert error.value.code == 401
    assert json.loads(error.value.read()) == {'error':'edge key required'}
    assert server.budget.used() == 0  # nothing reached the authority, nothing was spent
    # the key is a relay credential only: it must never reach the authority
    with pytest.raises(ValueError):
        EdgeForwarder(('127.0.0.1', 0), 'http://127.0.0.1:9', server.budget, ADMIN, ADMIN)


def test_remote_control_body_limit_refuses_before_forwarding(fleet):
    server, _t, url = fleet.make()
    request = urllib.request.Request(url+'/v1/workloads/worker/report', method='POST',
        data=b'x'*(64*1024+1), headers={'X-Edge-Key':KEY,'Content-Type':'application/json'})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 413
    assert json.loads(error.value.read()) == {'error':'control request byte limit exceeded'}
    assert server.budget.used() == 0


def test_edge_key_is_stripped_before_control_request_reaches_upstream(tmp_path):
    observed = {}

    class Capture(BaseHTTPRequestHandler):
        def do_POST(self):
            observed['path'] = self.path
            observed['edge_key'] = self.headers.get('X-Edge-Key')
            observed['authorization'] = self.headers.get('Authorization')
            self.rfile.read(int(self.headers.get('Content-Length','0')))
            self.send_response(200)
            self.send_header('Content-Length','2')
            self.send_header('Connection','close')
            self.end_headers()
            self.wfile.write(b'{}')
        def log_message(self, *_args): pass

    upstream = ThreadingHTTPServer(('127.0.0.1',0),Capture)
    upstream_thread = serve(upstream)
    budget = Budget(tmp_path/'budget.sqlite', 100_000)
    relay = EdgeForwarder(('127.0.0.1',0),f'http://127.0.0.1:{upstream.server_port}',
                          budget,ADMIN,KEY)
    relay_thread = serve(relay)
    try:
        request = urllib.request.Request(f'http://127.0.0.1:{relay.server_port}/v1/workloads/worker/status',
            method='POST',data=b'{}',headers={'X-Edge-Key':KEY,'Authorization':'Bearer '+'w'*32,
                                              'Content-Type':'application/json'})
        assert urllib.request.urlopen(request).read() == b'{}'
        assert observed == {'path':'/v1/workloads/worker/status','edge_key':None,
                            'authorization':'Bearer '+'w'*32}
    finally:
        stop(relay,relay_thread)
        stop(upstream,upstream_thread)


def test_budget_exhaustion_is_named_logged_and_falls_back(fleet, caplog):
    server, _t, url = fleet.make(cap=1000)
    server.budget.settle(0, 1000)  # the month's budget is spent
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(urllib.request.Request(
            f'{url}/v1/workloads/objects/{"f"*64}', headers={'Authorization': 'Bearer '+'a'*32, 'X-Edge-Key': KEY}))
    assert error.value.code == 503
    assert json.loads(error.value.read()) == {'error': 'budget_exhausted'}
    assert error.value.headers['X-Edge-Reason'] == 'budget_exhausted'
    public = json.loads(urllib.request.urlopen(url+'/v1/edge/status').read())
    assert public == {'state': 'budget_exhausted'}  # liveness only, no numbers
    private = json.loads(urllib.request.urlopen(urllib.request.Request(
        url+'/v1/edge/status', headers={'Authorization': 'Bearer '+ADMIN})).read())
    assert private['cap_bytes'] == 1000 and private['month']
    # The worker's transfer skips the relay by name and still succeeds.
    source, digest = blob(fleet.root, 200_000)
    caller = WorkloadClient(fleet.upstream, 'a'*32)
    uploaded = InputTransfer(caller).put(source)
    worker, assignment = claim(fleet.upstream, caller, uploaded)
    with caplog.at_level(logging.WARNING):
        got = InputTransfer(worker, relay=WorkloadClient(url, 'w'*32), relay_key=KEY).get(digest, fleet.root/'w/in.bin', assignment=assignment)
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    assert 'budget_exhausted' in caplog.text
    assert server.budget.used() == 1000  # nothing more was forwarded


def test_an_unreachable_upstream_or_relay_falls_back_to_the_authority(fleet, caplog):
    source, digest = blob(fleet.root, 100_000)
    caller = WorkloadClient(fleet.upstream, 'a'*32)
    uploaded = InputTransfer(caller).put(source)
    worker, assignment = claim(fleet.upstream, caller, uploaded)
    # Relay up, upstream dead: relay answers 502, download falls back.
    _s, _t, url = fleet.make(upstream_url='http://127.0.0.1:9')
    with caplog.at_level(logging.WARNING):
        got = InputTransfer(worker, relay=WorkloadClient(url, 'w'*32), relay_key=KEY).get(digest, fleet.root/'a/in.bin', assignment=assignment)
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    # Relay itself down.
    got = InputTransfer(worker, relay=WorkloadClient('http://127.0.0.1:9', 'w'*32), relay_key=KEY).get(
        digest, fleet.root/'b/in.bin', assignment=assignment)
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    assert 'unreachable' in caplog.text or 'relay' in caplog.text


def test_budget_counter_resets_by_month_and_is_bounded(tmp_path):
    now = [1_700_000_000.0]
    budget = Budget(tmp_path/'b.sqlite', 100, clock=lambda: now[0])
    assert budget.admit(10)
    budget.settle(10, 90)
    assert budget.exhausted() is False and budget.used() == 90
    assert not budget.admit(20)  # would pass the cap
    budget.settle(0, 20)
    assert budget.exhausted()
    for _ in range(6):  # six later months
        now[0] += 32*86400
        assert budget.exhausted() is False
        budget.settle(0, 1)
    rows = budget._db.execute('SELECT count(*) FROM budget').fetchone()[0]
    assert rows <= 3


def test_the_input_cache_fetches_through_the_relay(fleet):
    """The worker's real fetch path is InputCache, which used to rebuild its transfer from the
    client alone and so never used the relay: a green InputTransfer test proved nothing."""
    from livestack_node.workloads.input_cache import InputCache
    server, _t, url = fleet.make()
    source, digest = blob(fleet.root, 3_000_000)
    caller = WorkloadClient(fleet.upstream, 'a'*32)
    uploaded = InputTransfer(caller).put(source)
    worker, assignment = claim(fleet.upstream, caller, uploaded)
    cache = InputCache(fleet.root/'cache', InputTransfer(worker, relay=WorkloadClient(url, 'w'*32), relay_key=KEY),
                       max_bytes=50_000_000)
    path = cache.get(assignment)
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert server.budget.used() >= 3_000_000  # the bytes crossed the relay


def _download(fleet, url, digest, parallel, out_name='out.bin'):
    from livestack_node.workloads.download import download_into
    path = fleet.root/out_name
    with path.open('wb') as out:
        download_into(WorkloadClient(url, 'a'*32), digest, {'Authorization': 'Bearer '+'a'*32, 'X-Edge-Key': KEY},
                      out, 200*1024*1024, parallel=parallel)
    return path


def _counting(monkeypatch, fail_range_once=None, fail_times=1):
    """Wrap the REAL transport call: observe concurrency, optionally drop one block once."""
    import threading
    from livestack_node import transport
    real, state = transport.dial_stream, dict(now=0, peak=0, failed=False, failures=0, lock=threading.Lock())
    # dial_stream is a context manager: wrap it as one.
    import contextlib

    @contextlib.contextmanager
    def cm(target, method, path, headers=None, **kw):
        if fail_range_once and headers and headers.get('Range', '').startswith(fail_range_once) and state['failures'] < fail_times:
            state['failed'] = True
            state['failures'] += 1
            raise ConnectionError('injected: connection reset mid-block')
        with state['lock']:
            state['now'] += 1
            state['peak'] = max(state['peak'], state['now'])
        try:
            with real(target, method, path, headers=headers, **kw) as response:
                yield response
        finally:
            with state['lock']:
                state['now'] -= 1
    monkeypatch.setattr(transport, 'dial_stream', cm)
    return state


def test_parallel_blocks_fan_out_and_verify(fleet, monkeypatch):
    _s, _t, url = fleet.make()
    source, digest = blob(fleet.root, 21*1024*1024)  # six 4 MiB blocks
    InputTransfer(WorkloadClient(fleet.upstream, 'a'*32)).put(source)
    state = _counting(monkeypatch)
    got = _download(fleet, url, digest, parallel=4)
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    assert state['peak'] >= 2, 'blocks were fetched one at a time'


def test_a_failed_block_is_retried_in_place_and_keeps_the_fan_out(fleet, monkeypatch, caplog):
    _s, _t, url = fleet.make()
    source, digest = blob(fleet.root, 21*1024*1024)
    InputTransfer(WorkloadClient(fleet.upstream, 'a'*32)).put(source)
    state = _counting(monkeypatch, fail_range_once=f'bytes={2*4*1024*1024}-', fail_times=2)  # the third block, twice
    with caplog.at_level(logging.WARNING):
        got = _download(fleet, url, digest, parallel=4)
    assert state['failures'] == 2, 'the faults were never injected'
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    assert 'finishing sequentially' not in caplog.text, 'two drops must not end the fan-out'


def test_a_block_that_keeps_failing_hands_back_to_the_sequential_loop(fleet, monkeypatch, caplog):
    _s, _t, url = fleet.make()
    source, digest = blob(fleet.root, 21*1024*1024)
    InputTransfer(WorkloadClient(fleet.upstream, 'a'*32)).put(source)
    # BLOCK_RETRIES + 1 failures exhaust the in-place retries; the sequential loop then finishes it.
    state = _counting(monkeypatch, fail_range_once=f'bytes={2*4*1024*1024}-', fail_times=3)
    with caplog.at_level(logging.WARNING):
        got = _download(fleet, url, digest, parallel=4)
    assert state['failures'] == 3
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest
    assert 'finishing sequentially' in caplog.text


def test_more_workers_than_the_relay_admits_still_completes(fleet):
    # The relay admits 4 concurrent connections; asking for 8 gets some dropped. The download must
    # still finish with the right bytes, not fail and not corrupt.
    _s, _t, url = fleet.make()
    source, digest = blob(fleet.root, 33*1024*1024)
    InputTransfer(WorkloadClient(fleet.upstream, 'a'*32)).put(source)
    got = _download(fleet, url, digest, parallel=8)
    assert hashlib.sha256(got.read_bytes()).hexdigest() == digest


def test_a_one_use_upload_grant_put_reaches_the_authority_through_the_relay(fleet):
    _server, _t, url = fleet.make()
    source, digest = blob(fleet.root, 300*1024)
    size = source.stat().st_size
    grant = WorkloadClient(fleet.upstream, 'a'*32).request('upload-grants', dict(
        request_id='deploy-1', digest=digest, size=size, expires_in_seconds=600))
    relayed = grant['upload_url'].replace(fleet.upstream, url)
    request = urllib.request.Request(relayed, method='PUT', data=source.read_bytes(), headers={
        'Authorization': 'Bearer '+grant['capability'], 'Content-Type': 'application/octet-stream'})
    assert json.loads(urllib.request.urlopen(request).read())['digest'] == digest
    status = WorkloadClient(fleet.upstream, 'a'*32).request('upload-grants/deploy-1')
    assert status['state'] == 'uploaded'
    # Only PUT, and a wrong capability is still refused by the authority, not by the relay.
    for method in ('GET', 'POST'):
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(relayed, method=method, data=b'' if method == 'POST' else None))
        assert error.value.code == 405
    bad = urllib.request.Request(relayed, method='PUT', data=b'x', headers={'Authorization': 'Bearer '+'z'*40})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(bad)
    assert error.value.code in (401, 403)
