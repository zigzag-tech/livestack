"""Authority-wide wall-clock bounds on shared capacity: attempt length (3h) and queue age (24h)."""
import pytest

from livestack_node.workloads import causes, roster
from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.store import WorkloadStore

HOUR = 3600


def req(key, handler='test.v1', **extra):
    version = 4 if {'max_attempt_seconds'} & set(extra) else 1
    return dict(version=version, key=key, handler=handler, input_digest='a'*64, need={'cpu': 1}, **extra)


def register(store, worker='w1', host='h1'):
    return store.register(worker, host, 'b1', dict(capacity={'cpu': 8}, available={'cpu': 8}, labels={'os': 'linux'},
                                                   handlers=['test.v1', 'long.v1'], ready=True))


def advance(store, now, a, seconds, step=60):
    """Move the clock with the worker heartbeating, so the execution lease stays valid (the bound under test
    is wall clock, not lease expiry). Stops quietly once the authority has fenced the attempt."""
    end = now[0] + seconds
    while now[0] < end:
        now[0] = min(end, now[0] + step)
        try:
            store.heartbeat(a['worker'], a['boot'], a['attempt_id'], a['fence'])
        except WorkloadError:
            return


def make(tmp_path, **limits):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'a.db', handlers={'test.v1', 'long.v1'}, clock=lambda: now[0],
                          limits=Limits(**limits))
    register(store)
    return store, now


def test_defaults_are_three_hours_and_a_day(tmp_path):
    store, _ = make(tmp_path)
    assert store.limits.max_attempt_seconds == 3 * HOUR and store.limits.max_queue_age_seconds == 24 * HOUR
    assert store.limits.wall_clock_exceptions == {}


def test_attempt_at_2h59_survives_and_at_3h01_is_fenced_and_failed_with_a_named_cause(tmp_path):
    store, now = make(tmp_path)
    job = store.submit('o', req('k'))
    a = store.claim('w1', 'b1')
    advance(store, now, a, 3*HOUR - 60)
    assert store.get('o', job['id'])['state'] == 'running', '2h59 is inside the bound'
    advance(store, now, a, 120)
    done = store.get('o', job['id'])
    assert done['state'] == 'failed'
    assert done['cause']['kind'] == 'attempt_wall_clock_exceeded' and done['cause']['retry'] == 'after_change'
    assert done['cause']['evidence']['limit_seconds'] == 3*HOUR and done['cause']['evidence']['source'] == 'global'
    assert 'split' in done['reason']
    assert done['attempts'][0]['state'] == 'cleanup', 'fenced like any cancelled attempt: the worker is held until clean'
    with pytest.raises(WorkloadError, match='no longer valid'):
        store.heartbeat('w1', 'b1', a['attempt_id'], a['fence'])


def test_the_clock_is_per_attempt_not_per_job(tmp_path):
    store, now = make(tmp_path, attempts=2)
    job = store.submit('o', req('k'))
    first = store.claim('w1', 'b1')
    advance(store, now, first, 2*HOUR)
    store.complete(first['worker'], first['boot'], first['attempt_id'], first['fence'], input_digest='a'*64,
                   outcome='infrastructure', result={'error': 'abandoned', 'detail': 'execution lease expired'})
    register(store, 'w2', 'h2')  # placement avoids the worker an infrastructure failure just came from
    second = store.claim('w2', 'b1')
    assert second is not None
    advance(store, now, second, 2*HOUR)
    assert store.get('o', job['id'])['state'] == 'running', 'a retried attempt gets its own 3h'


def test_a_submitter_may_only_lower_the_bound(tmp_path):
    store, now = make(tmp_path)
    job = store.submit('o', req('low', max_attempt_seconds=600))
    a = store.claim('w1', 'b1')
    advance(store, now, a, 601)
    done = store.get('o', job['id'])
    assert done['state'] == 'failed' and done['cause']['evidence']['source'] == 'submitter'
    for bad in (3*HOUR + 1, 29, True, 'x'):
        with pytest.raises(WorkloadError, match='max_attempt_seconds'):
            store.submit('o', req('bad%s' % bad, max_attempt_seconds=bad))
    with pytest.raises(WorkloadError):
        store.submit('o', dict(req('v1'), max_attempt_seconds=600))  # needs schema 4


def test_exception_is_explicit_named_and_reasoned(tmp_path):
    exceptions = {'long.v1': {'max_attempt_seconds': 6*HOUR, 'reason': 'single indivisible training step'}}
    store, now = make(tmp_path, wall_clock_exceptions=exceptions)
    job = store.submit('o', req('k', handler='long.v1'))
    a = store.claim('w1', 'b1')
    advance(store, now, a, 5*HOUR)
    assert store.get('o', job['id'])['state'] == 'running'
    advance(store, now, a, 2*HOUR)
    assert store.get('o', job['id'])['cause']['evidence']['source'] == 'exception'
    other = store.submit('o', req('k2'))
    assert store.attempt_wall_clock({'handler': 'test.v1'}) == (3*HOUR, 'global'), 'other handlers keep the bound'
    assert other['state'] == 'queued'


@pytest.mark.parametrize('bad', [
    {'wall_clock_exceptions': {'x': {'max_attempt_seconds': 6*HOUR}}},                       # no reason
    {'wall_clock_exceptions': {'x': {'max_attempt_seconds': 6*HOUR, 'reason': 'short'}}},    # reason too thin
    {'wall_clock_exceptions': {'x': {'max_attempt_seconds': HOUR, 'reason': 'below the global bound'}}},
    {'wall_clock_exceptions': {'x': {'max_attempt_seconds': 90000, 'reason': 'above the hard ceiling'}}},
    {'wall_clock_exceptions': {'x': 'forever'}},
    {'max_attempt_seconds': None}, {'max_attempt_seconds': 0}, {'max_attempt_seconds': 100000},
    {'max_queue_age_seconds': None}, {'max_queue_age_seconds': True},
])
def test_the_bound_cannot_be_disabled_and_exceptions_need_a_reason(tmp_path, bad):
    with pytest.raises(ValueError):
        Limits(**bad)


def test_queued_job_expires_after_the_queue_bound_even_when_it_had_attempts(tmp_path):
    store, now = make(tmp_path, attempts=3)
    stuck = store.submit('o', req('stuck'))
    first = store.claim('w1', 'b1')
    store.complete(first['worker'], first['boot'], first['attempt_id'], first['fence'], input_digest='a'*64,
                   outcome='infrastructure', result={'error': 'abandoned', 'detail': 'execution lease expired'})
    # Not claimable again: the worker stops reporting, so the job waits queued.
    now[0] += 24*HOUR - 60
    assert store.get('o', stuck['id'])['state'] == 'queued', '23h59 is inside the bound'
    now[0] += 120
    done = store.get('o', stuck['id'])
    assert done['state'] == 'expired' and done['cause']['kind'] == 'queue_age_exceeded'
    assert done['cause']['evidence']['attempts'] == 1


def test_new_kinds_are_advertised_and_capabilities_state_the_bounds(tmp_path):
    store, _ = make(tmp_path)
    caps = store.capabilities('o', {'test.v1'})
    assert 'attempt_wall_clock_exceeded' in caps['causes']['kinds'] and 'queue_age_exceeded' in caps['causes']['kinds']
    assert caps['wall_clock'] == dict(version=1, max_attempt_seconds=3*HOUR, max_queue_age_seconds=24*HOUR, exceptions={})
    assert causes.validate_stored(causes.make('attempt_wall_clock_exceeded'))


def test_running_job_view_and_roster_show_the_bound_and_warn_near_it(tmp_path):
    store, now = make(tmp_path)
    job = store.submit('o', req('k'))
    a = store.claim('w1', 'b1')
    advance(store, now, a, int(2.5*HOUR))
    view = store.get('o', job['id'])
    assert view['wall_clock'] == dict(limit_seconds=3*HOUR, source='global', elapsed_seconds=2.5*HOUR,
                                      remaining_seconds=0.5*HOUR)
    store.register('w1', 'h1', 'b1', dict(capacity={'cpu': 8}, available={'cpu': 8}, labels={'os': 'linux'},
                                          handlers=['test.v1'], ready=True))
    doc = roster.build(store, [])
    kinds = {w['kind'] for w in doc['warnings']}
    assert 'attempt_near_wall_clock_bound' in kinds
    run = doc['workers'][0]['running'][0]
    assert run['wall_clock_limit_s'] == 3*HOUR


def test_no_warning_for_a_young_attempt(tmp_path):
    store, now = make(tmp_path)
    store.submit('o', req('k'))
    a = store.claim('w1', 'b1')
    advance(store, now, a, HOUR)
    assert not [w for w in roster.build(store, [])['warnings'] if w['kind'].endswith('bound')]
