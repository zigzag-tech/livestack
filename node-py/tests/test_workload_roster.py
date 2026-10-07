"""The fleet roster over a real authenticated authority (real HTTP, real SQLite store)."""
import hashlib
from io import BytesIO
import json
from threading import Thread
import urllib.error
import urllib.request

import pytest

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore


def _host(available=10, reserve=1):
    psi = {k: dict(full_avg10=0.0, full_avg60=0.0, some_avg10=0.0, some_avg60=0.0) for k in ('cpu', 'io', 'memory')}
    return dict(attempts={}, memory_available_bytes=available, memory_reserve_bytes=reserve,
                memory_total_bytes=100, psi=psi, services={}, swap_in_bytes_per_second=0.0)


def _report(handlers, **extra):
    host = _host()
    return dict(boot='b', report=dict(capacity={'cpu': 4, 'memory_bytes': 8}, available={'cpu': 4, 'memory_bytes': 8},
                labels=extra.get('labels', {}), handlers=handlers, ready=True, host=extra.get('host', host)))


@pytest.fixture
def api(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'a.v1', 'b.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('caller', 'c'*32, 'caller', ('a.v1',)),
        Principal('w-full', 'f'*32, 'worker', worker='w-full', host='h1'),
        Principal('w-part', 'p'*32, 'worker', worker='w-part', host='h1'),
        Principal('w-twin', 't'*32, 'worker', worker='w-twin', host='h1'),
        Principal('w-drain', 'd'*32, 'worker', worker='w-drain', host='h2', claim_enabled=False),
        Principal('w-new', 'n'*32, 'worker', worker='w-new', host='h3'),
    ])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def call(path, data=None, token='c'*32):
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/workloads/{path}',
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)
    yield call, store, server
    server.shutdown()
    thread.join(timeout=5)
    server.server_close()


def test_roster_merges_config_registration_and_running_and_names_disagreements(api):
    call, store, server = api
    call('worker/report', _report(['a.v1', 'b.v1'], labels={'x_handler_release': 'aaaa'}), token='f'*32)
    call('worker/report', _report(['a.v1'], labels={'x_handler_release': 'bbbb'}), token='p'*32)
    call('worker/report', _report(['a.v1'], labels={'x_handler_release': 'cccc'}), token='t'*32)
    call('worker/report', _report(['a.v1'], host=_host(0, 5)), token='d'*32)
    data = b'input'
    digest = hashlib.sha256(data).hexdigest()
    server.blobs.put('caller', digest, len(data), BytesIO(data))
    _, job = call('jobs', dict(version=1, key='k', handler='a.v1', input_digest=digest, need={'cpu': 1}))
    _, claim = call('worker/claim', {'boot': 'b'}, token='f'*32)
    assert claim['assignment']['job_id'] == job['id']
    status, roster = call('workers')
    assert status == 200
    workers = {w['id']: w for w in roster['workers']}
    assert set(workers) == {'w-full', 'w-part', 'w-twin', 'w-drain', 'w-new'}
    assert workers['w-full']['connected'] and workers['w-full']['state'] == 'running'
    assert workers['w-full']['running'][0]['job_id'] == job['id'] and workers['w-full']['running'][0]['handler'] == 'a.v1'
    assert workers['w-part']['eligible'] and workers['w-part']['state'] == 'idle'
    assert workers['w-new']['registered'] is False and workers['w-new']['ineligible_reasons'] == ['never_registered']
    assert workers['w-drain']['claim_enabled'] is False
    assert 'draining: claim_enabled=false' in workers['w-drain']['ineligible_reasons']
    assert 'memory_below_host_reserve' in workers['w-drain']['ineligible_reasons']
    kinds = {(d['worker'], d['kind']) for d in roster['disagreements']}
    assert ('w-new', 'configured_never_registered') in kinds
    assert ('w-part', 'fewer_handlers_than_peer') in kinds
    assert any(k == 'handler_release_skew' for _, k in kinds)
    assert 'token' not in json.dumps(roster)


def test_roster_marks_a_silent_worker_offline_and_is_not_readable_by_a_worker(api):
    call, store, server = api
    call('worker/report', _report(['a.v1']), token='f'*32)
    with store.transaction() as db:
        db.execute("UPDATE workers SET seen=seen-1000 WHERE id='w-full'")
    _, roster = call('workers')
    entry = next(w for w in roster['workers'] if w['id'] == 'w-full')
    assert entry['state'] == 'offline' and not entry['eligible'] and entry['last_seen_age_s'] >= 1000
    assert ('w-full', 'configured_silent') in {(d['worker'], d['kind']) for d in roster['disagreements']}
    assert call('workers', token='f'*32)[0] == 403
    assert call('workers', token='bad')[0] == 401


def test_roster_queue_names_who_holds_the_capacity_a_queued_job_needs(api):
    call, store, server = api
    for token in ('f', 'p'):
        call('worker/report', dict(boot='b', report=dict(capacity={'cpu': 4, 'memory_bytes': 8, 'disk_bytes': 100},
             available={'cpu': 4, 'memory_bytes': 8, 'disk_bytes': 100}, labels={}, handlers=['a.v1'], ready=True,
             host=_host())), token=token*32)
    for name, size in (('one', b'one'), ('two', b'two')):
        server.blobs.put('caller', hashlib.sha256(size).hexdigest(), len(size), BytesIO(size))
    first = call('jobs', dict(version=1, key='k1', handler='a.v1', input_digest=hashlib.sha256(b'one').hexdigest(),
                              need={'cpu': 3, 'disk_bytes': 60}))[1]
    assert call('worker/claim', {'boot': 'b'}, token='f'*32)[1]['assignment']['job_id'] == first['id']
    second = call('jobs', dict(version=1, key='k2', handler='a.v1', input_digest=hashlib.sha256(b'two').hexdigest(),
                               need={'cpu': 3, 'disk_bytes': 60}))[1]
    # w-part is idle on the SAME host: placement refuses it for what w-full already reserved.
    assert call('worker/claim', {'boot': 'b'}, token='p'*32)[1]['assignment'] is None
    status, roster = call('workers')
    assert status == 200 and [q['job_id'] for q in roster['queue']] == [second['id']]
    queued = roster['queue'][0]
    assert queued['handler'] == 'a.v1' and queued['age_s'] >= 0 and queued['admit']['cpu'] == 3
    assert queued['reason'], 'the authority own placement verdict is carried verbatim'
    by_worker = {w['worker']: w for w in queued['workers']}
    part = by_worker['w-part']
    assert any(r.startswith('cpu: needs 3, 1 free') and 'reserved by running attempts on host h1' in r
               for r in part['blocked_by'])
    assert any(r.startswith('disk: needs') for r in part['blocked_by'])
    assert part['host_holders'][0]['job_id'] == first['id'] and part['host_holders'][0]['worker'] == 'w-full'
    assert any(r.startswith('busy: holds attempt for job') for r in by_worker['w-full']['blocked_by'])
    assert queued['started_since'] == 0


def test_roster_queue_does_not_name_a_dead_workers_cleanup_attempt_as_a_holder(api):
    # Placement drops a `cleanup` hold once its worker has been silent for cleanup_seconds; the roster
    # must agree, or it names a blocker placement is not honouring.
    call, store, server = api
    for token in ('f', 'p'):
        call('worker/report', dict(boot='b', report=dict(capacity={'cpu': 4, 'memory_bytes': 8, 'disk_bytes': 100},
             available={'cpu': 4, 'memory_bytes': 8, 'disk_bytes': 100}, labels={}, handlers=['a.v1'], ready=True,
             host=_host())), token=token*32)
    for size in (b'one', b'two'):
        server.blobs.put('caller', hashlib.sha256(size).hexdigest(), len(size), BytesIO(size))
    first = call('jobs', dict(version=1, key='k1', handler='a.v1', input_digest=hashlib.sha256(b'one').hexdigest(),
                              need={'cpu': 3, 'disk_bytes': 60}))[1]
    assert call('worker/claim', {'boot': 'b'}, token='f'*32)[1]['assignment']['job_id'] == first['id']
    call('jobs', dict(version=1, key='k2', handler='a.v1', input_digest=hashlib.sha256(b'two').hexdigest(),
                      need={'cpu': 3, 'disk_bytes': 60}))
    with store.transaction() as db:
        db.execute("UPDATE attempts SET state='cleanup' WHERE job=?", (first['id'],))
        db.execute("UPDATE workers SET seen=seen-?", (store.limits.cleanup_seconds + 10,))
        db.execute("UPDATE workers SET seen=seen+? WHERE id='w-part'", (store.limits.cleanup_seconds + 10,))
    _, roster = call('workers')
    part = {w['worker']: w for w in roster['queue'][0]['workers']}['w-part']
    assert part['host_holders'] == [], 'a stale cleanup attempt is not a capacity holder'


def test_roster_queue_is_empty_when_nothing_waits(api):
    call, _, _ = api
    assert call('workers')[1]['queue'] == []


def test_roster_says_what_each_running_and_queued_job_is_about(api):
    call, store, server = api
    call('worker/report', _report(['a.v1']), token='f'*32)
    for size in (b'one', b'two'):
        server.blobs.put('caller', hashlib.sha256(size).hexdigest(), len(size), BytesIO(size))
    first = call('jobs', dict(version=1, key='k1', handler='a.v1', input_digest=hashlib.sha256(b'one').hexdigest(),
                              need={'cpu': 4}, payload=dict(purpose='admission', phase='e2e', source_commit='0123456789abcdef',
                              selection=['x.one', 'x.two', 'x.three', 'x.four'])))[1]
    assert call('worker/claim', {'boot': 'b'}, token='f'*32)[1]['assignment']['job_id'] == first['id']
    second = call('jobs', dict(version=1, key='k2', handler='a.v1', input_digest=hashlib.sha256(b'two').hexdigest(),
                               need={'cpu': 4}, labels={'describe': 'Hand-written description', 'origin': 'agent:claude@h1'}))[1]
    _, roster = call('workers')
    running = next(w for w in roster['workers'] if w['id'] == 'w-full')['running'][0]
    assert running['describe'] == 'admission 012345678: x.one, x.two (+2)' and running['origin'] is None
    queued = next(q for q in roster['queue'] if q['job_id'] == second['id'])
    assert queued['describe'] == 'Hand-written description' and queued['origin'] == 'agent:claude@h1'


def test_about_synthesizes_release_jobs_truncates_and_never_reads_secrets():
    from livestack_node.workloads.roster import _about
    about = _about(dict(handler='r.v1', payload=dict(component='hub', build_number=3377, plan_id='69c88856-e13b',
                        signer_sha256='SECRET'), labels={'describe': 'x'*500}))
    assert len(about['describe']) == 120
    release = _about(dict(handler='r.v1', payload=dict(component='hub', build_number=3377, plan_id='69c88856-e13b')))
    assert release['describe'] == 'release hub build 3377 plan 69c88856'
    assert 'SECRET' not in json.dumps(_about(dict(handler='r.v1', payload=dict(component='app', signer_sha256='SECRET'))))


def test_warnings_flag_an_idle_worker_that_could_take_waiting_work_but_names_no_reason():
    # Direct on the pure function: a real authority would claim this job, so no live state can show it.
    from livestack_node.workloads.roster import _warnings
    idle = dict(id='w1', host='h', connected=True, state='idle', eligible=True, activation_failures=[])
    broken = dict(id='w2', host='h', connected=True, state='idle', eligible=False,
                  activation_failures=['generation 16: handler_runtime_not_installed: python3'])
    queue = [dict(job_id='j1', handler='a.v1', describe='d', age_s=600.0,
                  workers=[dict(worker='w1', host='h', blocked_by=[]), dict(worker='w3', host='h', blocked_by=['busy'])]),
             dict(job_id='j2', handler='a.v1', describe='young', age_s=5.0, workers=[dict(worker='w1', host='h', blocked_by=[])])]
    found = _warnings([idle, broken], queue)
    assert [(w['kind'], w['worker']) for w in found] == [('activation_failed_idle', 'w2'),
                                                         ('idle_while_claimable_work_waits', 'w1')]
    assert found[1]['job_id'] == 'j1' and 'handler_runtime_not_installed' in found[0]['detail']
