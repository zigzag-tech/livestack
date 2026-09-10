"""Real SQLite transactions and independent clients, no emulated lease store."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.store import WorkloadStore


@pytest.fixture
def harness(tmp_path):
    now = [1000.0]
    path = tmp_path / 'authority.db'
    store = WorkloadStore(path, handlers={'test.v1', 'build.v1'}, clock=lambda: now[0])
    return store, now, path


def request(key='one', *, handler='test.v1', need=None, **extra):
    return dict(version=1, key=key, handler=handler, input_digest='a'*64,
                need=need or {'cpu': 2, 'ram': 4}, **extra)


def register(store, worker='w1', host='host1', boot='boot1', *, cpu=4, ram=8, handlers=None, cleaned=()):
    return store.register(worker, host, boot, dict(
        capacity={'cpu': cpu, 'ram': ram}, available={'cpu': cpu, 'ram': ram},
        labels={'os': 'linux'}, handlers=handlers or ['test.v1', 'build.v1'], ready=True), cleaned=cleaned)


def complete(store, a, outcome='succeeded', **kwargs):
    return store.complete(a['worker'], a['boot'], a['attempt_id'], a['fence'],
                          input_digest=kwargs.get('digest', 'a'*64), outcome=outcome,
                          result=kwargs.get('result', {'exit': 0}))


def test_durable_idempotent_submission_and_principal_scope(harness):
    store, now, path = harness
    job = store.submit('alice', request())
    reopened = WorkloadStore(path, handlers={'test.v1'}, clock=lambda: now[0])
    assert reopened.submit('alice', request())['id'] == job['id']
    assert reopened.submit('bob', request())['id'] != job['id']
    with pytest.raises(WorkloadError, match='different inputs'):
        reopened.submit('alice', request(payload={'changed': True}))
    with pytest.raises(WorkloadError, match='not found'):
        reopened.get('bob', job['id'])
    assert 'priority' not in job['spec'], 'legacy request identity stays byte-compatible'


def test_independent_connections_do_not_double_reserve_physical_ram(harness):
    store, now, path = harness
    register(store, 'linux', 'same-physical-host', cpu=16, ram=8)
    register(store, 'wsl', 'same-physical-host', cpu=16, ram=8)
    a = store.submit('owner', request('test', need={'cpu': 2, 'ram': 6}))
    b = store.submit('owner', request('build', handler='build.v1', need={'cpu': 2, 'ram': 6}))
    def claim(worker):
        client = WorkloadStore(path, handlers={'test.v1', 'build.v1'}, clock=lambda: now[0])
        return client.claim(worker, 'boot1')
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(claim, ['linux', 'wsl']))
    assert len([c for c in claims if c]) == 1
    states = [store.get('owner', j['id'])['state'] for j in [a,b]]
    assert sorted(states) == ['queued', 'running']
    held = next(c for c in claims if c)
    assert claim(held['worker'])['attempt_id'] == held['attempt_id']


def test_two_physical_hosts_can_execute_concurrently(harness):
    store, _, _ = harness
    register(store)
    register(store, 'w2', 'host2')
    for k in ['one', 'two']:
        store.submit('owner', request(k))
    a, b = store.claim('w1', 'boot1'), store.claim('w2', 'boot1')
    assert a and b and a['job_id'] != b['job_id']


def test_later_high_priority_job_is_claimed_before_older_batch_work(harness):
    store, now, _ = harness
    register(store)
    older = store.submit('owner', request('older'))
    now[0] += 1
    urgent = store.submit('owner', request('urgent', priority=12000))
    claimed = store.claim('w1', 'boot1')
    assert claimed['job_id'] == urgent['id']
    assert store.get('owner', older['id'])['state'] == 'queued'


def test_placement_excludes_workers_without_requested_handler(harness):
    store, _, _ = harness
    register(store, handlers=['build.v1'])
    job = store.submit('owner', request())
    assert store.claim('w1', 'boot1') is None
    observed = store.get('owner', job['id'])
    assert observed['state'] == 'queued'
    assert observed['reason'] == 'no fresh worker advertises handler test.v1'
    assert 'w1' not in observed['reason']


def test_placement_refusal_lists_only_compatible_workers(harness):
    store, _, _ = harness
    register(store, 'runner', 'runner-host', boot='runner-boot', handlers=['test.v1'])
    first = store.submit('owner', request('running'))
    assert store.claim('runner', 'runner-boot')['job_id'] == first['id']
    register(store, 'stager', 'stager-host', boot='stager-boot', handlers=['build.v1'])
    waiting = store.submit('owner', request('waiting'))
    assert store.claim('stager', 'stager-boot') is None
    reason = store.get('owner', waiting['id'])['reason']
    assert 'runner' in reason and 'worker holds an active attempt or cleanup' in reason
    assert 'stager' not in reason and 'handler not installed' not in reason


def test_authority_restart_requires_reconciliation_and_keeps_claim(harness):
    store, now, path = harness
    register(store, ram=4)
    j = store.submit('owner', request())
    a = store.claim('w1', 'boot1')
    reopened = WorkloadStore(path, handlers={'test.v1'}, clock=lambda: now[0])
    reopened.recover()
    assert reopened.claim('w1', 'boot1') is None
    register(reopened, ram=4, handlers=['test.v1'])
    assert reopened.claim('w1', 'boot1')['attempt_id'] == a['attempt_id']
    assert complete(reopened, a)['state'] == 'succeeded'
    assert reopened.get('owner', j['id'])['result']['result'] == {'exit': 0}


def test_expiry_fences_results_and_retains_capacity_until_cleanup(harness):
    store, now, _ = harness
    register(store)
    job = store.submit('owner', request())
    a = store.claim('w1', 'boot1')
    now[0] += 121
    register(store, 'w2', 'host2')
    b = store.claim('w2', 'boot1')
    assert b['fence'] == a['fence'] + 1
    with pytest.raises(WorkloadError, match='fenced'):
        complete(store, a)
    report = register(store)
    assert not report['ready']
    assert report['cleanup'] == [a['attempt_id']]
    assert store.claim('w1', 'boot1') is None
    assert register(store, cleaned=[a['attempt_id']])['ready']
    assert complete(store, b)['state'] == 'succeeded'
    assert store.get('owner', job['id'])['fence'] == 2


def test_job_deadline_expires_queued_and_fences_running_work(harness):
    store, now, _ = harness
    queued = store.submit('owner', request('queued-deadline', deadline=now[0] + 5))
    now[0] += 6
    assert store.get('owner', queued['id'])['state'] == 'expired'
    assert store.get('owner', queued['id'])['reason'] == 'execution deadline expired'

    register(store)
    running = store.submit('owner', request('running-deadline', deadline=now[0] + 4000))
    attempt = store.claim('w1', 'boot1')
    assert attempt['job_id'] == running['id']
    now[0] += 4001
    assert store.get('owner', running['id'])['state'] == 'expired'
    with pytest.raises(WorkloadError, match='fenced'):
        complete(store, attempt)
    report = register(store)
    assert not report['ready']
    assert report['cleanup'] == [attempt['attempt_id']]


def test_queued_job_expires_when_estimate_no_longer_fits_deadline(harness):
    store, now, _ = harness
    queued = store.submit('owner', request(
        'queued-fit', deadline=now[0] + 100, estimate_seconds=90))
    now[0] += 10
    assert store.get('owner', queued['id'])['state'] == 'queued', (
        'an estimate that exactly fits the remaining window is still schedulable')
    now[0] += 1
    expired = store.get('owner', queued['id'])
    assert expired['state'] == 'expired'
    assert expired['reason'] == 'estimated execution cannot fit remaining deadline (89s < 90s)'
    assert expired['attempts'] == [], 'placement expiry must not invent execution evidence'


def test_running_job_keeps_its_absolute_deadline_after_estimate_window_erodes(harness):
    store, now, _ = harness
    register(store)
    running = store.submit('owner', request(
        'running-fit', deadline=now[0] + 100, estimate_seconds=90))
    attempt = store.claim('w1', 'boot1')
    assert attempt['job_id'] == running['id']
    now[0] += 20
    assert store.get('owner', running['id'])['state'] == 'running', (
        'the estimate is an admission bound, not a new deadline for work already running')
    assert complete(store, attempt)['state'] == 'succeeded'


def test_worker_boot_change_does_not_free_owned_processes(harness):
    store, _, _ = harness
    register(store)
    store.submit('owner', request())
    a = store.claim('w1', 'boot1')
    assert not register(store, boot='boot2')['ready']
    with pytest.raises(WorkloadError, match='session'):
        store.heartbeat('w1', 'boot1', a['attempt_id'], a['fence'])
    assert register(store, boot='boot2', cleaned=[a['attempt_id']])['ready']
    b = store.claim('w1', 'boot2')
    assert b['fence'] == 2


def test_only_infrastructure_retries_and_three_attempt_limit(harness):
    store, _, _ = harness
    register(store)
    job = store.submit('owner', request())
    for fence in range(1, 4):
        a = store.claim('w1', 'boot1')
        assert a['fence'] == fence
        done = complete(store, a, 'infrastructure')
        assert done['state'] == ('queued' if fence < 3 else 'failed')
        assert complete(store, a, 'infrastructure')['state'] == done['state']
    assert store.claim('w1', 'boot1') is None
    assert len(store.get('owner', job['id'])['attempts']) == 3
    j = store.submit('owner', request('product-failure'))
    a = store.claim('w1', 'boot1')
    assert complete(store, a, 'product_failure')['state'] == 'failed'
    assert len(store.get('owner', j['id'])['attempts']) == 1


def test_wrong_digest_or_modified_completion_is_rejected(harness):
    store, _, _ = harness
    register(store)
    store.submit('owner', request())
    a = store.claim('w1', 'boot1')
    with pytest.raises(WorkloadError, match='digest'):
        complete(store, a, digest='b'*64)
    assert complete(store, a)['state'] == 'succeeded'
    assert complete(store, a)['state'] == 'succeeded'
    with pytest.raises(WorkloadError, match='fenced'):
        complete(store, a, result={'exit': 1})


def test_cancel_fences_but_requires_cleanup(harness):
    store, _, _ = harness
    register(store)
    j = store.submit('owner', request())
    a = store.claim('w1', 'boot1')
    assert store.cancel('owner', j['id'])['state'] == 'cancelled'
    with pytest.raises(WorkloadError, match='valid'):
        store.heartbeat('w1', 'boot1', a['attempt_id'], a['fence'])
    assert not register(store)['ready']
    assert register(store, cleaned=[a['attempt_id']])['ready']


def test_stale_or_missing_capacity_and_missing_capability_never_grant(harness):
    store, now, _ = harness
    register(store)
    store.submit('owner', request(selector={'signer': 'shipping'}))
    assert store.claim('w1', 'boot1') is None
    store.submit('owner', request('disk', need={'scratch': 1}))
    assert store.claim('w1', 'boot1') is None
    store.submit('owner', request('fits'))
    now[0] += 61
    assert store.claim('w1', 'boot1') is None


def test_records_and_retention_exemptions_are_bounded(tmp_path):
    now = [1000.0]
    s = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: now[0],
                      limits=Limits(active_jobs=1, terminal_jobs=1, terminal_seconds=10))
    register(s, handlers=['test.v1'])
    kept = s.submit('owner', request('kept', retain=True))
    with pytest.raises(WorkloadError, match='capacity'):
        s.submit('owner', request('overflow'))
    complete(s, s.claim('w1', 'boot1'))
    forgotten = s.submit('owner', request('forget'))
    complete(s, s.claim('w1', 'boot1'))
    now[0] += 11
    s.sweep()
    assert s.get('owner', kept['id'])['state'] == 'succeeded'
    with pytest.raises(WorkloadError, match='not found'):
        s.get('owner', forgotten['id'])
    with pytest.raises(WorkloadError, match='byte limit'):
        s.submit('owner', request('huge', payload={'value': 'x'*65536}))


def test_unconfigured_deletion_preserves_data_but_admission_stays_bounded(tmp_path):
    s = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'},
                      limits=Limits(active_jobs=1, terminal_jobs=1, terminal_seconds=None))
    for key in ['a', 'b']:
        j = s.submit('owner', request(key))
        s.cancel('owner', j['id'])
    s.sweep()
    with pytest.raises(WorkloadError, match='capacity'):
        s.submit('owner', request('c'))


@pytest.mark.parametrize('extra', [dict(need={'cpu': -1}), dict(need={'cpu': float('nan')}),
                                  dict(need={'cpu': True}), dict(handler='shell'),
                                  dict(payload=[]), dict(version=2), dict(command='rm -rf /'),
                                  dict(priority=True), dict(priority=1.5), dict(priority=1_000_001)])
def test_invalid_requests_cannot_become_execution(harness, extra):
    s, _, _ = harness
    data = request()
    data.update(extra)
    with pytest.raises(WorkloadError):
        s.submit('owner', data)
