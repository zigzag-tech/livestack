"""Work scopes against a real SQLite authority and a real HTTP server (openspec work-scopes-and-cascade-cancel)."""
import json
import sqlite3
from threading import Thread
import urllib.error
import urllib.request

import pytest

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.store import WorkloadStore


def req(key, scope=None, **extra):
    body = dict(version=4 if scope else 1, key=key, handler='test.v1', input_digest='a'*64, need={'cpu': 1}, **extra)
    if scope:
        body['scope'] = scope
    return body


def register(store, worker='w1', host='h1', boot='b1'):
    return store.register(worker, host, boot, dict(capacity={'cpu': 8}, available={'cpu': 8}, labels={'os': 'linux'},
                                                    handlers=['test.v1'], ready=True))


@pytest.fixture
def harness(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'a.db', handlers={'test.v1'}, clock=lambda: now[0])
    return store, now, tmp_path/'a.db'


def test_close_cascades_queued_and_running_and_holds_worker(harness):
    store, now, _ = harness
    register(store)
    running = store.submit('o', req('r', {'key': 'run:1'}))
    assignment = store.claim('w1', 'b1')
    queued = store.submit('o', req('q', {'key': 'run:1'}))
    done = store.submit('o', req('d', {'key': 'run:1'}))
    other = store.submit('o', req('x', {'key': 'run:2'}))
    unscoped = store.submit('o', req('u'))
    result = store.close_scope('o', 'run:1', 'plan failed')
    assert (result['cancelled'], result['running_cleanup'], result['replayed']) == (3, 1, False)
    assert store.get('o', running['id'])['state'] == 'cancelled'
    assert store.get('o', queued['id'])['state'] == 'cancelled'
    assert 'run:1 closed: plan failed' in store.get('o', queued['id'])['reason']
    assert store.get('o', other['id'])['state'] == 'queued', 'another scope is untouched'
    assert store.get('o', unscoped['id'])['state'] == 'queued'
    attempt = store.get('o', running['id'])['attempts'][0]
    assert attempt['state'] == 'cleanup', 'the worker is held until it reports clean, exactly as owner cancel'
    assert store.claim('w1', 'b1') is None or store.claim('w1', 'b1')['attempt_id'] == assignment['attempt_id']


def test_close_is_idempotent_and_unknown_scope_is_created_closed(harness):
    store, _, _ = harness
    store.submit('o', req('a', {'key': 's'}))
    first = store.close_scope('o', 's', 'why')
    second = store.close_scope('o', 's', 'other reason')
    assert second['replayed'] and second['cancelled'] == first['cancelled']
    assert store.get_scope('o', 's')['close_reason'] == 'why'
    store.close_scope('o', 'never-seen', 'cancel before first submit')
    with pytest.raises(WorkloadError, match='scope_closed') as raised:
        store.submit('o', req('late', {'key': 'never-seen'}))
    assert raised.value.status == 409


def test_replay_into_closed_scope_is_refused_not_returned(harness):
    store, _, _ = harness
    job = store.submit('o', req('k', {'key': 's'}))
    assert store.submit('o', req('k', {'key': 's'}))['id'] == job['id']
    store.close_scope('o', 's')
    with pytest.raises(WorkloadError, match='scope_closed') as raised:
        store.submit('o', req('k', {'key': 's'}))
    assert raised.value.status == 409
    with pytest.raises(WorkloadError, match='scope_closed'):
        store.submit('o', req('fresh', {'key': 's'}))


def test_same_key_other_scope_is_a_conflicting_request(harness):
    store, _, _ = harness
    store.submit('o', req('k', {'key': 's1'}))
    with pytest.raises(WorkloadError, match='different inputs'):
        store.submit('o', req('k', {'key': 's2'}))


def test_lease_expiry_closes_with_cascade_and_renew_extends(harness):
    store, now, _ = harness
    job = store.submit('o', req('k', {'key': 's', 'lease_seconds': 300}))
    now[0] += 299
    store.renew_scope('o', 's')
    now[0] += 299
    assert store.get('o', job['id'])['state'] == 'queued', 'a renewal moved the expiry'
    now[0] += 2
    assert store.get('o', job['id'])['state'] == 'cancelled'
    view = store.get_scope('o', 's')
    assert (view['state'], view['close_reason'], view['closed_by']) == ('closed', 'lease expired', 'authority')
    with pytest.raises(WorkloadError, match='scope_closed'):
        store.renew_scope('o', 's')


def test_lease_default_and_bounds(harness):
    store, now, _ = harness
    store.submit('o', req('k', {'key': 's'}))
    assert store.get_scope('o', 's')['lease_seconds'] == 1800
    for bad in (299, 14401, 0, True, 'x'):
        with pytest.raises(WorkloadError):
            store.submit('o', req('b%s' % bad, {'key': 'bad', 'lease_seconds': bad}))
    store.submit('o', req('lo', {'key': 'lo', 'lease_seconds': 300}))
    store.submit('o', req('hi', {'key': 'hi', 'lease_seconds': 14400}))


def test_expiry_cascade_survives_a_refusal_in_the_same_call(harness):
    store, now, path = harness
    store.submit('o', req('k', {'key': 's', 'lease_seconds': 300}))
    now[0] += 301
    with pytest.raises(WorkloadError, match='scope_closed'):
        store.submit('o', req('k', {'key': 's', 'lease_seconds': 300}))
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT state FROM scopes").fetchone()[0] == 'closed', 'committed despite the refusal'


def test_unknown_scope_is_404_and_empty_scope_reports_zeros(harness):
    store, _, _ = harness
    with pytest.raises(WorkloadError) as raised:
        store.get_scope('o', 'nope')
    assert raised.value.status == 404
    job = store.submit('o', req('k', {'key': 's'}))
    store.cancel('o', job['id'])
    view = store.get_scope('o', 's')
    assert view['jobs']['cancelled'] == 1 and view['jobs']['queued'] == 0


def test_capacity_refusals(tmp_path):
    store = WorkloadStore(tmp_path/'a.db', handlers={'test.v1'}, limits=Limits(scope_jobs=2, scopes_per_owner=2))
    store.submit('o', req('a', {'key': 's'}))
    store.submit('o', req('b', {'key': 's'}))
    with pytest.raises(WorkloadError, match='scope_capacity') as raised:
        store.submit('o', req('c', {'key': 's'}))
    assert raised.value.status == 429
    store.submit('o', req('d', {'key': 't'}))
    with pytest.raises(WorkloadError, match='scope_capacity'):
        store.submit('o', req('e', {'key': 'u'}))
    store.submit('p', req('e', {'key': 'u'}))  # per owner


def test_scope_required_handler_and_old_versions(tmp_path):
    store = WorkloadStore(tmp_path/'a.db', handlers={'test.v1'}, limits=Limits(scope_required_handlers=('test.v1',)))
    with pytest.raises(WorkloadError, match='scope_required'):
        store.submit('o', req('a'))
    store.submit('o', req('a', {'key': 's'}))
    with pytest.raises(WorkloadError, match='unsupported'):
        WorkloadStore(tmp_path/'b.db', handlers={'test.v1'}).submit('o', dict(req('x', {'key': 's'}), version=3))


def test_migration_of_a_database_that_predates_scopes(tmp_path):
    path = tmp_path/'old.db'
    WorkloadStore(path, handlers={'test.v1'}).submit('o', req('a'))
    with sqlite3.connect(path) as db:
        db.execute("DROP INDEX jobs_scope")
        db.execute("ALTER TABLE jobs DROP COLUMN scope")
        db.execute("DROP TABLE scopes")
    store = WorkloadStore(path, handlers={'test.v1'})
    store.submit('o', req('b', {'key': 's'}))
    assert store.get_scope('o', 's')['jobs']['queued'] == 1


def test_closed_scopes_are_pruned_only_after_their_jobs_and_the_window(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'a.db', handlers={'test.v1'}, clock=lambda: now[0], limits=Limits(terminal_seconds=1000))
    job = store.submit('o', req('a', {'key': 's'}))
    store.close_scope('o', 's')
    now[0] += 500
    store.sweep()
    assert store.get_scope('o', 's')['state'] == 'closed'
    assert store.get('o', job['id'])['state'] == 'cancelled'
    now[0] += 600
    store.sweep()
    with pytest.raises(WorkloadError, match='not found'):
        store.get('o', job['id'])
    with pytest.raises(WorkloadError, match='scope_not_found'):
        store.get_scope('o', 's')


@pytest.fixture
def api(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('alice', 'a'*32, 'caller', ('test.v1',)), Principal('bob', 'b'*32, 'caller', ('test.v1',))])
    server.blobs.put('alice', __import__('hashlib').sha256(b'x').hexdigest(), 1, __import__('io').BytesIO(b'x'))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, data=None, token='a'*32):
        r = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/workloads/{path}',
                                   data=json.dumps(data).encode() if data is not None else None,
                                   headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(r, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
    yield call
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_http_round_trip_and_principal_isolation(api):
    digest = __import__('hashlib').sha256(b'x').hexdigest()
    body = dict(version=4, key='k', handler='test.v1', input_digest=digest, need={'cpu': 1},
                scope={'key': 'zzops:app:run:1', 'lease_seconds': 600})
    status, job = api('jobs', body)
    assert status == 200 and job['scope'] == 'zzops:app:run:1'
    assert api('capabilities')[1]['scopes']['lease_max_seconds'] == 14400
    status, view = api('scopes/zzops:app:run:1')
    assert status == 200 and view['jobs']['queued'] == 1 and view['lease_seconds'] == 600
    assert api('scopes/zzops:app:run:1', token='b'*32)[0] == 404
    assert api('scopes/zzops:app:run:1/close', {}, token='b'*32)[1].get('replayed') is False, 'bob closes bob\'s own namespace'
    assert api('scopes/zzops:app:run:1')[1]['state'] == 'open'
    assert api('scopes/zzops:app:run:1/renew', {})[0] == 200
    status, closed = api('scopes/zzops:app:run:1/close', {'reason': 'done'})
    assert (status, closed['cancelled']) == (200, 1)
    assert api('scopes/zzops:app:run:1/renew', {})[0] == 409
    status, refused = api('jobs', body)
    assert status == 409 and refused['error'].startswith('scope_closed')
    assert api('jobs/'+job['id'])[1]['state'] == 'cancelled'
