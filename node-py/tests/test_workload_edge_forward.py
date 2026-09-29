"""The edge relay in front of a REAL authority: no fakes on either side.

It must forward object routes and nothing else, hold no bytes, keep the
authority's authorization, refuse over budget by name, and never leave a worker
without a path (fall back to the authority)."""
import hashlib
import json
import logging
import os
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
        Principal('owner', 'a'*32, 'caller', ('test.v1',)),
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


def test_only_object_routes_are_forwarded(fleet):
    _s, _t, url = fleet.make()
    for path, method in (('/v1/workloads/jobs', 'GET'), ('/v1/workloads/worker/claim', 'POST'), ('/v1/fleet/admit', 'GET')):
        request = urllib.request.Request(url+path, method=method, data=b'{}' if method == 'POST' else None,
                                         headers={'Authorization': 'Bearer '+'a'*32})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request)
        assert error.value.code == 404
    digest = 'f'*64
    request = urllib.request.Request(f'{url}/v1/workloads/objects/{digest}', method='DELETE')
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 405


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
    assert server.budget.used() == 0  # nothing reached the authority, nothing was spent
    # the key is a relay credential only: it must never reach the authority
    with pytest.raises(ValueError):
        EdgeForwarder(('127.0.0.1', 0), 'http://127.0.0.1:9', server.budget, ADMIN, ADMIN)


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
