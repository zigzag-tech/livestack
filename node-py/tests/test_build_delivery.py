"""Exercise placement through the real durable authority and SQLite ledger."""
import pytest

from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore


def worker(store, ident, host, *, cpu=4, available=4, local=False, limit=None):
    report = dict(capacity={'cpu': cpu}, available={'cpu': available}, handlers=['compile.v1'],
                  labels={'os': 'linux'}, ready=True, local_artifacts=local)
    if limit is not None:
        report['execution_limit'] = {'cpu': limit}
    store.register(ident, host, 'boot', report)


def request(key, **extra):
    return dict(version=1, key=key, handler='compile.v1', input_digest='a'*64,
                need={'cpu': 2}, estimate_seconds=600, resource_profile='linux-debug-x64',
                delivery=dict(consumer_host='laptop', expected_output_bytes=400*1024**2), **extra)


def store_at(tmp_path):
    return WorkloadStore(tmp_path/'jobs.db', handlers={'compile.v1'}, clock=lambda: 1000)


def test_local_handoff_wins_without_discarding_durable_request_identity(tmp_path):
    store = store_at(tmp_path)
    worker(store, 'a-remote', 'builder')
    worker(store, 'z-local', 'laptop', local=True)
    job = store.submit('owner', request('one'))
    assert store.submit('owner', request('one'))['id'] == job['id']
    assert store.claim('a-remote', 'boot') is None
    assignment = store.claim('z-local', 'boot')
    assert assignment['job_id'] == job['id']
    reopened = store_at(tmp_path)
    assert reopened.get('owner', job['id'])['spec']['delivery']['consumer_host'] == 'laptop'


@pytest.mark.parametrize('local_case', ['busy', 'execution-limit', 'insufficient'])
def test_local_preference_does_not_override_headroom_or_execution_limits(tmp_path, local_case):
    store = store_at(tmp_path)
    worker(store, 'remote', 'builder')
    worker(store, 'local', 'laptop', local=True,
           available=0 if local_case == 'busy' else 4,
           cpu=1 if local_case == 'insufficient' else 4,
           limit=1 if local_case == 'execution-limit' else None)
    store.submit('owner', request('one'))
    assert store.claim('remote', 'boot') is not None
    assert store.claim('local', 'boot') is None


def test_profile_history_and_measured_route_can_favor_a_remote_build(tmp_path):
    store = store_at(tmp_path)
    worker(store, 'remote', 'builder')
    worker(store, 'local', 'laptop', local=True)
    with store.transaction() as db:
        from livestack_node.workloads.build_delivery import history_key
        profile = history_key('compile.v1', 'linux-debug-x64')
        db.execute('INSERT INTO build_history(profile,host,attempt,seconds,at) VALUES(?,?,?,?,?)',
                   (profile, 'laptop', 'old-local', 1000, 999))
        db.execute('INSERT INTO build_history(profile,host,attempt,seconds,at) VALUES(?,?,?,?,?)',
                   (profile, 'builder', 'old-remote', 10, 999))
    value = request('one')
    value['delivery']['routes'] = [dict(host='builder', bytes_per_second=100*1024**2, measured_at=999)]
    store.submit('owner', value)
    assert store.claim('remote', 'boot') is not None


def test_missing_local_handoff_does_not_claim_a_zero_cost_binary_transfer(tmp_path):
    store = store_at(tmp_path)
    worker(store, 'a-remote', 'builder')
    worker(store, 'z-local', 'laptop', local=False)
    value = request('one')
    value['delivery']['routes'] = [dict(host='builder', bytes_per_second=100*1024**2, measured_at=999)]
    store.submit('owner', value)
    assert store.claim('a-remote', 'boot') is not None


def test_invalid_delivery_and_profile_identity_changes_are_explicit(tmp_path):
    store = store_at(tmp_path)
    value = request('bad'); value['delivery']['expected_output_bytes'] = True
    with pytest.raises(WorkloadError, match='output size'):
        store.submit('owner', value)
    value = request('one'); store.submit('owner', value)
    value['resource_profile'] = 'android-apk-x64'
    with pytest.raises(WorkloadError, match='different inputs'):
        store.submit('owner', value)


def _history(store, host, seconds, attempt):
    from livestack_node.workloads.build_delivery import history_key
    with store.transaction() as db:
        db.execute('INSERT INTO build_history(profile,host,attempt,seconds,at) VALUES(?,?,?,?,?)',
                   (history_key('compile.v1', 'linux-release-arm64'), host, attempt, seconds, 999))


def release(key):
    # A release compile: a resource profile, no delivery intent (its result goes to the shared store).
    return dict(version=1, key=key, handler='compile.v1', input_digest='a'*64,
                need={'cpu': 2}, estimate_seconds=600, resource_profile='linux-release-arm64')


def test_profile_without_delivery_prefers_the_host_with_the_faster_measured_build(tmp_path):
    store = store_at(tmp_path)
    worker(store, 'a-slow', 'old-box')
    worker(store, 'z-fast', 'new-box')
    _history(store, 'old-box', 900, 'old-1')
    _history(store, 'new-box', 400, 'new-1')
    job = store.submit('owner', release('one'))
    assert store.claim('a-slow', 'boot') is None
    assert store.claim('z-fast', 'boot')['job_id'] == job['id']


def test_profile_without_delivery_falls_back_when_the_faster_host_is_full(tmp_path):
    store = store_at(tmp_path)
    worker(store, 'a-slow', 'old-box')
    worker(store, 'z-fast', 'new-box', available=0)
    _history(store, 'old-box', 900, 'old-1')
    _history(store, 'new-box', 400, 'new-1')
    job = store.submit('owner', release('one'))
    assert store.claim('a-slow', 'boot')['job_id'] == job['id']


def test_unmeasured_host_is_charged_the_slowest_known_build(tmp_path):
    store = store_at(tmp_path)
    worker(store, 'a-unmeasured', 'spare-box')
    worker(store, 'm-slow', 'old-box')
    worker(store, 'z-fast', 'new-box')
    _history(store, 'old-box', 900, 'old-1')
    _history(store, 'new-box', 400, 'new-1')
    job = store.submit('owner', release('one'))
    assert store.claim('a-unmeasured', 'boot') is None
    assert store.claim('z-fast', 'boot')['job_id'] == job['id']


def test_profile_without_any_history_keeps_scheduler_order(tmp_path):
    store = store_at(tmp_path)
    worker(store, 'a-first', 'box-a')
    worker(store, 'z-second', 'box-b')
    job = store.submit('owner', release('one'))
    # Nothing measured: the scheduler's own choice stands, exactly as for a job with no profile.
    assert store.claim('z-second', 'boot') is None
    assert store.claim('a-first', 'boot')['job_id'] == job['id']
