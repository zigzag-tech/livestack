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


def register(store, worker, view, *, hostname='zz-joe', memory=31*GIB, boot='boot1', cpu=16):
    capacity = dict(cpu=cpu, memory_bytes=memory, disk_bytes=500*GIB)
    report = dict(capacity=capacity, available=dict(capacity, memory_bytes=min(memory, 30*GIB)),
                  labels={'os': 'linux'}, handlers=[E2E, 'small.v1'], ready=True)
    if view is not None:
        report['host'] = view
    return store.register(worker, hostname, boot, report)


def e2e(store, key):
    return store.submit('owner', dict(version=1, key=key, handler=E2E, input_digest='a'*64,
        need=dict(cpu=6, memory_bytes=10*GIB, disk_bytes=16*GIB),
        admit=dict(cpu=2, memory_bytes=4*GIB, disk_bytes=16*GIB)))


def learn(store, handler, peak, key='learn', need=10*GIB, outcome='succeeded', field='memory_peak_bytes'):
    """Record one completed attempt of `handler` with its cgroup memory peak, the
    way the worker's completion receipt (resource_usage.py) carries it."""
    register(store, 'teacher', None, hostname='elsewhere')
    store.submit('owner', dict(version=1, key=key, handler=handler, input_digest='a'*64,
                               need=dict(cpu=6, memory_bytes=need, disk_bytes=16*GIB)))
    a = store.claim('teacher', 'boot1')
    store.complete('teacher', 'boot1', a['attempt_id'], a['fence'], input_digest='a'*64, outcome=outcome,
                   result=dict(exit_code=0 if outcome == 'succeeded' else 75, artifacts=[], resources={field: peak}))
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


def test_only_succeeded_attempts_teach_a_peak(store):
    """An attempt that died in preparation peaks low; on 2026-10-02 twenty such
    infrastructure failures in a row would have taught e2e 3.8 GiB."""
    learn(store, E2E, 3*GIB, key='died-early', outcome='infrastructure')
    register(store, 'w', host(8*GIB))
    job = e2e(store, 'one')
    assert store.claim('w', 'boot1') is None
    assert 'claim 10.0 GiB' in store.get('owner', job['id'])['reason'], 'unlearned: need'


@pytest.mark.parametrize('resident, charged', [(True, 0.0), (False, 13.0), (None, 13.0)])
def test_a_model_server_spike_is_charged_unless_all_its_units_are_resident(store, resident, charged):
    """klein-0 resident cannot reload, so its 13 GiB transient is not
    outstanding; non-resident, or unreadable (null), it is charged."""
    klein = {KLEIN: dict(current_bytes=3*GIB, peak_bytes=16*GIB, resident=resident)}
    register(store, 'w', host(20*GIB, services=klein))
    job = e2e(store, 'one')
    claim = store.claim('w', 'boot1')
    if charged:
        assert claim is None
        assert f'model servers {charged:.1f}' in store.get('owner', job['id'])['reason']
    else:
        assert claim['job_id'] == job['id'], store.get('owner', job['id'])['reason']


def test_the_non_reclaimable_peak_is_preferred_over_the_cache_inclusive_one(store):
    """memory_peak_bytes counts page cache (e2e runs hit their 10 GiB cap that
    way). Once a succeeded attempt carries the worker's non-reclaimable peak,
    that figure is the claim; the old one is only the fallback."""
    learn(store, E2E, 10*GIB, key='old')
    learn(store, E2E, 6*GIB, key='new', field='memory_nonreclaimable_peak_bytes')
    register(store, 'w', host(8*GIB))
    job = e2e(store, 'one')
    assert store.claim('w', 'boot1')['job_id'] == job['id'], store.get('owner', job['id'])['reason']


# --- a job that states no `admit` is charged what its handler measured -----------
# 2026-10-11: release builds declared only `need`, so admit defaulted to need and a
# measured 1.3 GiB build was charged its 3 GiB cap for the whole attempt.

def plain(store, key, need):
    return store.submit('owner', dict(version=1, key=key, handler=E2E, input_digest='a'*64,
        need=dict(cpu=6, memory_bytes=need, disk_bytes=16*GIB)))


def learn_many(store, peak, count, need, field='memory_nonreclaimable_peak_bytes'):
    for i in range(count):
        learn(store, E2E, peak, key=f'{need}-{peak}-{i}', need=need, field=field)


def test_a_job_with_no_admit_is_charged_its_measured_peak_with_margin(store):
    learn_many(store, 2*GIB, 5, need=8*GIB)
    register(store, 'w', host(4*GIB))  # 3 GiB after the reserve: fits 2.5 GiB, not the 8 GiB cap
    job = plain(store, 'one', 8*GIB)
    assert store.claim('w', 'boot1')['job_id'] == job['id'], store.get('owner', job['id'])['reason']


def test_a_job_with_no_admit_is_charged_need_until_enough_samples(store):
    learn_many(store, 2*GIB, 4, need=8*GIB)
    register(store, 'w', host(4*GIB))
    job = plain(store, 'one', 8*GIB)
    assert store.claim('w', 'boot1') is None
    assert 'claim 8.0 GiB' in store.get('owner', job['id'])['reason']


def test_samples_run_under_a_smaller_need_do_not_lower_a_larger_need_claim(store):
    learn_many(store, 2*GIB, 5, need=8*GIB)  # the x64 daemon
    register(store, 'w', host(8*GIB))
    job = plain(store, 'arm64', 16*GIB)  # the arm64 daemon: no samples of its own
    assert store.claim('w', 'boot1') is None
    assert 'claim 16.0 GiB' in store.get('owner', job['id'])['reason']


def test_the_margin_never_charges_more_than_need(store):
    learn_many(store, 7*GIB, 5, need=8*GIB)  # 7 x 1.25 = 8.75 GiB
    register(store, 'w', host(8*GIB + GIB))
    job = plain(store, 'one', 8*GIB)
    assert store.claim('w', 'boot1')['job_id'] == job['id'], store.get('owner', job['id'])['reason']


def test_a_stated_admit_still_floors_the_claim(store):
    learn_many(store, 1*GIB, 5, need=10*GIB)
    register(store, 'w', host(4*GIB + GIB - 1))
    job = e2e(store, 'one')  # admit 4 GiB
    assert store.claim('w', 'boot1') is None
    assert 'claim 4.0 GiB' in store.get('owner', job['id'])['reason']


# --- cores: a job that states no cpu `admit` is charged its measured PEAK ---------
# 2026-10-11: four attempts on zz-joe reserved 10 declared cores while using ~1-1.2 each,
# and a release build waited for cores that were idle. `plain` needs 6 cores, no admit.

def learn_cores(store, peak, count, key='cores', field='cpu_peak_cores'):
    for i in range(count):
        learn(store, E2E, peak, key=f'{key}-{peak}-{i}', need=10*GIB, field=field)


def test_a_job_with_no_admit_is_charged_its_measured_peak_cores_with_margin(store):
    learn_cores(store, 1.0, 5)
    register(store, 'w', host(20*GIB), cpu=2)  # 6 declared cores cannot fit; 1.25 can
    job = plain(store, 'one', 10*GIB)
    assert store.claim('w', 'boot1')['job_id'] == job['id'], store.get('owner', job['id'])['reason']


def test_cores_are_charged_need_until_enough_peak_samples(store):
    learn_cores(store, 1.0, 4)
    register(store, 'w', host(20*GIB), cpu=2)
    job = plain(store, 'one', 10*GIB)
    assert store.claim('w', 'boot1') is None
    assert 'insufficient shared host resources' in store.get('owner', job['id'])['reason']


def test_the_mean_is_not_a_peak(store):
    """cpu_usage_usec / execution_seconds is a mean; a burst-heavy build reaches several
    times it. Only the sampled window peak lowers a claim."""
    for i in range(6):
        register(store, 'teacher', None, hostname='elsewhere')
        store.submit('owner', dict(version=1, key=f'mean{i}', handler=E2E, input_digest='a'*64,
                                   need=dict(cpu=6, memory_bytes=10*GIB, disk_bytes=16*GIB)))
        a = store.claim('teacher', 'boot1')
        store.complete('teacher', 'boot1', a['attempt_id'], a['fence'], input_digest='a'*64, outcome='succeeded',
                       result=dict(exit_code=0, artifacts=[], resources=dict(
                           cpu_usage_usec=1_000_000, execution_seconds=10.0, memory_peak_bytes=GIB)))
    register(store, 'w', host(20*GIB), cpu=2)
    plain(store, 'one', 10*GIB)
    assert store.claim('w', 'boot1') is None


def test_the_cores_claim_has_a_floor(store):
    learn_cores(store, 0.1, 5)  # 0.125 cores measured
    register(store, 'w', host(20*GIB), cpu=0.5)
    job = plain(store, 'one', 10*GIB)
    assert store.claim('w', 'boot1') is None, 'a build is never charged under one core'


def test_peaks_measured_under_a_smaller_cpu_need_are_not_borrowed(store):
    learn_cores(store, 1.0, 5)  # ran under cpu need 6
    register(store, 'w', host(20*GIB), cpu=2)
    store.submit('owner', dict(version=1, key='big', handler=E2E, input_digest='a'*64,
                               need=dict(cpu=8, memory_bytes=10*GIB, disk_bytes=16*GIB)))
    assert store.claim('w', 'boot1') is None


def test_a_running_attempt_is_charged_its_measured_cores_not_its_stored_declaration(store):
    learn_cores(store, 0.8, 5)  # claim 1.0 each
    register(store, 'w1', host(40*GIB), cpu=2)
    register(store, 'w2', host(40*GIB), cpu=2)
    first, second = plain(store, 'one', 10*GIB), plain(store, 'two', 10*GIB)
    claimed = {store.claim('w1', 'boot1')['job_id']}
    other = store.claim('w2', 'boot1')
    assert other, [store.get('owner', j['id'])['reason'] for j in (first, second)]
    claimed.add(other['job_id'])
    assert claimed == {first['id'], second['id']}
    third = plain(store, 'three', 10*GIB)
    register(store, 'w3', host(40*GIB), cpu=2)
    assert store.claim('w3', 'boot1') is None  # 2 cores are now claimed
    assert store.get('owner', third['id'])['state'] == 'queued'


def test_a_stated_cpu_admit_is_charged_as_stated(store):
    learn_cores(store, 0.5, 5)
    register(store, 'w', host(20*GIB), cpu=1.5)
    job = e2e(store, 'one')  # admit cpu 2
    assert store.claim('w', 'boot1') is None
    assert 'insufficient shared host resources' in store.get('owner', job['id'])['reason']
