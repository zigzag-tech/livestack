"""Placement on a measured host: real SQLite store, real worker reports.

openspec/changes/host-memory-ledger. On 2026-10-02 zz-joe (31 GB) ran two e2e
attempts charged 4 GiB `admit` each while they reached 10 GiB, beside an image
model server whose host-RAM transient peaks at 16 GB. It swapped 17 GiB and every
attempt there failed at startup.
"""
import pytest

from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore

GIB = 1024**3
E2E = 'benchday.e2e.full.v1'
KLEIN = 'system.slice/harmony-klein-0.service'


@pytest.fixture
def store(tmp_path):
    now = [1000.0]
    return WorkloadStore(tmp_path/'authority.db', handlers={E2E, 'small.v1'}, clock=lambda: now[0])


def host(available, *, services=None, attempts=None, full60=0.5, swap=0.0, reserve=GIB):
    window = dict(some_avg10=0.0, some_avg60=0.0, full_avg10=0.0, full_avg60=full60)
    return dict(memory_total_bytes=31*GIB, memory_available_bytes=available, memory_reserve_bytes=reserve,
                swap_in_bytes_per_second=swap, psi=dict(memory=window, io=window, cpu=window),
                attempts=attempts or {}, services=services or {})


def register(store, worker, view, *, hostname='zz-joe', memory=31*GIB, boot='boot1'):
    capacity = dict(cpu=16, memory_bytes=memory, disk_bytes=500*GIB)
    report = dict(capacity=capacity, available=dict(capacity, memory_bytes=min(memory, 30*GIB)),
                  labels={'os': 'linux'}, handlers=[E2E, 'small.v1'], ready=True)
    if view is not None:
        report['host'] = view
    return store.register(worker, hostname, boot, report)


def e2e(store, key):
    return store.submit('owner', dict(version=1, key=key, handler=E2E, input_digest='a'*64,
        need=dict(cpu=6, memory_bytes=10*GIB, disk_bytes=16*GIB),
        admit=dict(cpu=2, memory_bytes=4*GIB, disk_bytes=16*GIB)))


def learn(store, handler, peak, key='learn', need=10*GIB):
    """Record one completed attempt of `handler` with its cgroup memory peak, the
    way the worker's completion receipt (resource_usage.py) carries it."""
    register(store, 'teacher', None, hostname='elsewhere')
    store.submit('owner', dict(version=1, key=key, handler=handler, input_digest='a'*64,
                               need=dict(cpu=1, memory_bytes=need, disk_bytes=GIB)))
    a = store.claim('teacher', 'boot1')
    store.complete('teacher', 'boot1', a['attempt_id'], a['fence'], input_digest='a'*64, outcome='succeeded',
                   result=dict(exit_code=0, artifacts=[], resources=dict(memory_peak_bytes=peak)))
    # The teacher leaves the roster so it cannot take the jobs under test.
    store.register('teacher', 'elsewhere', 'boot1', dict(
        capacity=dict(cpu=1), available=dict(cpu=1), labels={}, handlers=[handler], ready=False))


def test_the_2026_10_02_shape_admits_one_e2e_attempt_not_two(store):
    """Positive control: the placement before this change admitted both (4 + 4
    GiB of admit fit 20 GiB per identity), which is the overcommit that swapped."""
    learn(store, E2E, 10*GIB)
    klein = {KLEIN: dict(current_bytes=3*GIB, peak_bytes=16*GIB)}
    register(store, 'zz-joe-e2e-1', host(27*GIB, services=klein))
    register(store, 'zz-joe-e2e-2', host(27*GIB, services=klein))
    first, second = e2e(store, 'one'), e2e(store, 'two')
    granted = [c for c in (store.claim('zz-joe-e2e-1', 'boot1'), store.claim('zz-joe-e2e-2', 'boot1')) if c]
    assert len(granted) == 1
    waiting = second if granted[0]['job_id'] == first['id'] else first
    reason = store.get('owner', waiting['id'])['reason']
    # 27 available - 1 reserve - 13 klein transient - 10 for the first attempt
    # (admitted, no cgroup yet) = 3 GiB.
    assert 'insufficient host memory: claim 10.0 GiB > free 3.0 GiB' in reason, reason
    assert 'running attempts 10.0' in reason and 'model servers 13.0' in reason, reason


def test_a_running_attempt_is_charged_only_what_it_has_not_yet_used(store):
    """MemAvailable already contains a running attempt's current use; charging
    its whole claim again would double-count it."""
    learn(store, E2E, 10*GIB)
    register(store, 'zz-joe-e2e-1', host(30*GIB))
    register(store, 'zz-joe-e2e-2', host(30*GIB))
    e2e(store, 'one')
    running = store.claim('zz-joe-e2e-1', 'boot1')
    # The attempt has grown to 9 GiB of its 10 GiB claim; the host reports 21 GiB
    # available. Free = 21 - 1 - (10 - 9) = 19 GiB: room for a second attempt.
    register(store, 'zz-joe-e2e-2', host(21*GIB, attempts={running['attempt_id']: 9*GIB}))
    second = e2e(store, 'two')
    assert store.claim('zz-joe-e2e-2', 'boot1')['job_id'] == second['id']


def test_an_attempt_admitted_but_not_started_is_charged_its_whole_claim(store):
    learn(store, E2E, 10*GIB)
    register(store, 'zz-joe-e2e-1', host(20*GIB))
    register(store, 'zz-joe-e2e-2', host(20*GIB))
    e2e(store, 'one')
    assert store.claim('zz-joe-e2e-1', 'boot1')
    second = e2e(store, 'two')
    register(store, 'zz-joe-e2e-2', host(20*GIB))  # no cgroup for the first attempt yet
    assert store.claim('zz-joe-e2e-2', 'boot1') is None
    assert 'claim 10.0 GiB > free 9.0 GiB' in store.get('owner', second['id'])['reason']


def test_an_unlearned_handler_is_charged_need_and_a_learned_one_its_peak(store):
    register(store, 'w', host(8*GIB))
    job = e2e(store, 'one')
    assert store.claim('w', 'boot1') is None, 'unlearned: need (10 GiB) > 7 GiB free'
    assert 'claim 10.0 GiB' in store.get('owner', job['id'])['reason']
    store.cancel('owner', job['id'])
    learn(store, E2E, 5*GIB, key='learned')
    register(store, 'w', host(8*GIB))
    job = e2e(store, 'two')
    assert store.claim('w', 'boot1')['job_id'] == job['id'], store.get('owner', job['id'])['reason']


def test_a_learned_peak_never_falls_below_admit(store):
    learn(store, E2E, 1*GIB)
    register(store, 'w', host(4*GIB + GIB - 1))  # 4 GiB minus a byte after the reserve
    job = e2e(store, 'one')
    assert store.claim('w', 'boot1') is None
    assert 'claim 4.0 GiB' in store.get('owner', job['id'])['reason']


def test_memory_pressure_defers_admission_and_names_it(store):
    register(store, 'w', host(28*GIB, full60=7.1))
    job = e2e(store, 'one')
    assert store.claim('w', 'boot1') is None
    assert 'host memory pressure: memory full avg60 7.1% (limit 5%)' in store.get('owner', job['id'])['reason']
    register(store, 'w', host(28*GIB, swap=40*1024**2))
    assert store.claim('w', 'boot1') is None
    assert 'swap-in 40.0 MiB/s' in store.get('owner', job['id'])['reason']
    register(store, 'w', host(28*GIB))
    assert store.claim('w', 'boot1')['job_id'] == job['id']


def test_unknown_pressure_is_not_pressure(store):
    view = host(28*GIB, swap=None)
    view['psi'] = dict(memory=None, io=None, cpu=None)
    register(store, 'w', view)
    job = e2e(store, 'one')
    assert store.claim('w', 'boot1')['job_id'] == job['id']


def test_a_configured_capacity_still_caps_an_identity(store):
    register(store, 'w', host(28*GIB), memory=8*GIB)
    job = e2e(store, 'one')
    assert store.claim('w', 'boot1') is None
    assert 'claim 10.0 GiB > free 8.0 GiB' in store.get('owner', job['id'])['reason']


def test_a_host_without_a_measured_view_is_placed_as_before(store):
    """Back-compatibility: workers not yet upgraded keep the admit-vector charge."""
    learn(store, E2E, 10*GIB)
    register(store, 'a', None, memory=20*GIB)
    register(store, 'b', None, memory=20*GIB)
    e2e(store, 'one'), e2e(store, 'two')
    granted = [c for c in (store.claim('a', 'boot1'), store.claim('b', 'boot1')) if c]
    assert len(granted) == 2


@pytest.mark.parametrize('mutate', [
    lambda v: v.pop('psi'),
    lambda v: v.update(memory_available_bytes=-1),
    lambda v: v.update(attempts={'not-an-attempt': 1}),
    lambda v: v.update(services={KLEIN: dict(current_bytes=1)}),
    lambda v: v.update(extra=1),
])
def test_a_malformed_host_block_is_refused(store, mutate):
    view = host(8*GIB)
    mutate(view)
    with pytest.raises(WorkloadError, match='host report'):
        register(store, 'w', view)
