"""Live capacity and durable host health in workload placement.

zz-leaky (2026-11-14..): non-ECC DIMMs flipping bit 60, sticks swapped one at a time, so the RAM its
workers measure changes on every boot while the memory verdict is still open. Two separate facts:

- capacity is whatever the host reports NOW (MemTotal/MemAvailable from the worker's `host` block,
  of the current kernel boot, inside the freshness window); no inventory table is edited;
- health is an owner's verdict, held durably per physical host; no capacity report, boot, claims
  tick or drain TTL clears it.

Real SQLite store and real HTTP server throughout, as in test_workload_host_memory/test_workload_claims.
"""
import threading

import pytest

from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore

from test_workload_claims import ADMIN, ROLL, W1, Authority, principals

GIB = 1024**3
E2E = 'benchday.e2e.full.v1'
BOOT_A, BOOT_B = 'a'*8 + '-0000-4000-8000-' + 'a'*12, 'b'*8 + '-0000-4000-8000-' + 'b'*12


@pytest.fixture
def clock():
    return [1000.0]


@pytest.fixture
def store(tmp_path, clock):
    return WorkloadStore(tmp_path/'authority.db', handlers={E2E}, clock=lambda: clock[0])


def host(available, total=31*GIB, boot_id=BOOT_A):
    window = dict(some_avg10=0.0, some_avg60=0.0, full_avg10=0.0, full_avg60=0.5)
    view = dict(memory_total_bytes=total, memory_available_bytes=available, memory_reserve_bytes=GIB,
                swap_in_bytes_per_second=0.0, psi=dict(memory=window, io=window, cpu=window),
                attempts={}, services={})
    if boot_id is not None:
        view['boot_id'] = boot_id
    return view


def register(store, worker, view, *, hostname='zz-leaky', boot='s1'):
    """A worker with no configured `capacity`: it offers the measured MemTotal (worker.report)."""
    capacity = dict(cpu=32, memory_bytes=view['memory_total_bytes'], disk_bytes=200*GIB)
    report = dict(capacity=capacity, available=dict(capacity), labels={'os': 'linux'}, handlers=[E2E], ready=True,
                  host=view)
    return store.register(worker, hostname, boot, report)


def job(store, key, memory=10*GIB):
    return store.submit('owner', dict(version=1, key=key, handler=E2E, input_digest='a'*64,
                                      need=dict(cpu=2, memory_bytes=memory, disk_bytes=GIB)))


def reason(store, submitted):
    return store.get('owner', submitted['id'])['reason'] or ''


def test_ram_increase_admits_without_an_inventory_edit(store):
    register(store, 'leaky-1', host(8*GIB, total=30*GIB))
    waiting = job(store, 'big')
    assert store.claim('leaky-1', 's1') is None
    assert 'insufficient host memory' in reason(store, waiting)
    # Sticks added and the machine rebooted: the worker's next report is the only input.
    register(store, 'leaky-1', host(110*GIB, total=123*GIB, boot_id=BOOT_B), boot='s2')
    assert store.claim('leaky-1', 's2')['job_id'] == waiting['id']


def test_ram_decrease_stops_new_placements_but_keeps_the_running_attempt(store):
    register(store, 'leaky-1', host(100*GIB, total=123*GIB))
    register(store, 'leaky-2', host(100*GIB, total=123*GIB))
    first = store.claim('leaky-1', 's1') or None
    assert first is None  # nothing queued yet
    running = job(store, 'one')
    attempt = store.claim('leaky-1', 's1')
    assert attempt['job_id'] == running['id']
    # A stick is pulled: the host now reports 30 GiB total, 9 GiB free.
    register(store, 'leaky-2', host(9*GIB, total=30*GIB))
    second = job(store, 'two')
    assert store.claim('leaky-2', 's1') is None
    assert 'insufficient host memory' in reason(store, second)
    assert store.get('owner', running['id'])['state'] == 'running'  # never killed


def test_a_stale_heartbeat_offers_no_capacity(store, clock):
    register(store, 'leaky-1', host(100*GIB, total=123*GIB))
    clock[0] += store.limits.fresh_seconds + 1
    waiting = job(store, 'late')
    assert store.claim('leaky-1', 's1') is None  # its last report is outside the freshness window
    assert store.get('owner', waiting['id'])['state'] == 'queued'
    register(store, 'leaky-1', host(100*GIB, total=123*GIB))  # a fresh heartbeat brings it back
    assert store.claim('leaky-1', 's1')['job_id'] == waiting['id']


def test_a_report_from_before_a_reboot_is_stale_capacity(store):
    """Both identities report under boot A with plenty of RAM. The host reboots with half its sticks;
    identity 2 re-registers under boot B (9 GiB free). Identity 1 is still inside the freshness window
    with its boot-A report. It must not be placed on the old boot's RAM."""
    register(store, 'leaky-1', host(100*GIB, total=123*GIB))
    register(store, 'leaky-2', host(100*GIB, total=123*GIB))
    register(store, 'leaky-2', host(9*GIB, total=30*GIB, boot_id=BOOT_B), boot='s2')
    waiting = job(store, 'after-reboot')
    assert store.claim('leaky-1', 's1') is None
    placement = store.get('owner', waiting['id'])
    codes = {(b['worker'], b['code']) for b in placement['placement']['blockers']}
    assert ('leaky-1', 'host_capacity_stale') in codes, placement
    assert 'free 8.0 GiB (available 9.0' in placement['reason']  # the post-reboot measurement is the host view
    # Re-registering under the new boot replaces the stale report; the measured 9 GiB still decides.
    register(store, 'leaky-1', host(9*GIB, total=30*GIB, boot_id=BOOT_B), boot='s3')
    assert store.claim('leaky-1', 's3') is None
    assert 'insufficient host memory' in reason(store, waiting)


def test_a_held_host_survives_capacity_growth_reboot_and_the_claims_tick(store, clock):
    from livestack_node.workloads.host_health import HostHealth
    health = HostHealth(store)
    health.hold('zz-leaky', owner='memory-test', reason='bit-60 flips under diagnosis')
    register(store, 'leaky-1', host(100*GIB, total=123*GIB))
    waiting = job(store, 'held')
    assert store.claim('leaky-1', 's1') is None
    assert 'host_health_held by memory-test' in reason(store, waiting)
    # Every capacity/boot change the DIMM swaps produce; a day of drain-TTL ticks.
    for n, (total, boot_id, session) in enumerate(((30*GIB, BOOT_B, 's2'), (123*GIB, BOOT_A, 's3'),
                                                   (61*GIB, None, 's4'))):
        clock[0] += 25 * 3600
        register(store, 'leaky-1', host(total - 4*GIB, total=total, boot_id=boot_id), boot=session)
        waiting = job(store, f'held-{n}', memory=GIB)  # a fresh job each day: queued jobs may age out
        assert store.claim('leaky-1', session) is None
        assert 'host_health_held by memory-test' in reason(store, waiting)
    assert health.get('zz-leaky')['held'] is True
    # Only the owner of the verdict (or a forced admin) releases it.
    with pytest.raises(WorkloadError, match='host_health_held_by:memory-test'):
        health.release('zz-leaky', owner='someone-else', reason='looks fine')
    health.release('zz-leaky', owner='memory-test', reason='all-four soak clean')
    assert store.claim('leaky-1', 's4')['job_id'] == waiting['id']


def test_hosts_other_than_the_held_one_are_untouched(store):
    from livestack_node.workloads.host_health import HostHealth
    HostHealth(store).hold('zz-leaky', owner='memory-test', reason='diagnosis')
    register(store, 'whitby-1', host(100*GIB, total=123*GIB), hostname='zz-whitby')
    submitted = job(store, 'elsewhere')
    assert store.claim('whitby-1', 's1')['job_id'] == submitted['id']


def test_two_identities_racing_for_one_hosts_memory_get_one_placement(store):
    """Assignment/resource race: 20 GiB free, two 10 GiB-plus-reserve claims, two threads."""
    register(store, 'leaky-1', host(20*GIB, total=30*GIB))
    register(store, 'leaky-2', host(20*GIB, total=30*GIB))
    job(store, 'one'); job(store, 'two')
    got, barrier = [], threading.Barrier(2)

    def claim(worker):
        barrier.wait()
        got.append(store.claim(worker, 's1'))

    threads = [threading.Thread(target=claim, args=(w,)) for w in ('leaky-1', 'leaky-2')]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len([g for g in got if g]) == 1


# --- the HTTP surface, through a real WorkloadServer ------------------------------------------

@pytest.fixture
def authority(tmp_path):
    made = Authority(tmp_path, principals(workers=(('w1', W1, 'zz-leaky'),)))
    yield made
    made.close()


def test_hold_covers_an_unregistered_worker_and_needs_an_owner_to_release(authority):
    held = authority.call(ADMIN, 'hosts/zz-leaky/health/hold', {'owner': 'memory-test', 'reason': 'verdict open'})
    assert held['held'] is True and held['generation'] == 1
    # w1 never registered; it is withheld already (assignment, capacity counts, roster all read this).
    roster = {w['id']: w for w in authority.client(ROLL).request('workers')['workers']}
    assert roster['w1']['claim_enabled'] is False
    worker = authority.client(W1)
    worker.request('worker/report', dict(boot='b1', report=dict(
        capacity={'cpu': 2}, available={'cpu': 2}, labels={}, handlers=['test.v1'], ready=True)))
    authority.client('a'*32).submit(dict(version=1, key='k', handler='test.v1', input_digest=authority.digest,
                                         need={'cpu': 1}))
    assert worker.request('worker/claim', {'boot': 'b1'})['assignment'] is None
    roster = {w['id']: w for w in authority.client(ROLL).request('workers')['workers']}
    assert any('host_health_held by memory-test' in r for r in roster['w1']['ineligible_reasons'])
    # The reconciler may hold but never release; a different owner needs force.
    assert authority.refused(ROLL, 'hosts/zz-leaky/health/release',
                             {'owner': 'memory-test', 'reason': 'x'}).status == 403
    assert authority.refused(ADMIN, 'hosts/zz-leaky/health/release',
                             {'owner': 'ops', 'reason': 'x'}).status == 409
    assert authority.refused(ADMIN, 'hosts/zz-leaky/health/hold', {'owner': 'memory-test'}).status == 400
    released = authority.call(ADMIN, 'hosts/zz-leaky/health/release',
                              {'owner': 'memory-test', 'reason': 'verdict: clean'})
    assert released['held'] is False and released['generation'] == 2
    assert worker.request('worker/claim', {'boot': 'b1'})['assignment'] is not None
    assert authority.call(ADMIN, 'hosts/health')['hosts'][0]['state'] == 'eligible'


def test_hostview_reports_the_kernel_boot_and_the_validator_bounds_it(tmp_path):
    from livestack_node.hostview import boot_id
    from livestack_node.workloads.model import host_view
    (tmp_path/'sys/kernel/random').mkdir(parents=True)
    (tmp_path/'sys/kernel/random/boot_id').write_text(BOOT_A + '\n')
    assert boot_id(str(tmp_path)) == BOOT_A
    assert boot_id(str(tmp_path/'missing')) is None  # unknown is None, never a made-up id
    assert host_view(host(GIB))['boot_id'] == BOOT_A
    assert 'boot_id' not in host_view(host(GIB, boot_id=None))  # older workers omit it
    with pytest.raises(WorkloadError):
        host_view(dict(host(GIB), boot_id='../../etc'))
    with pytest.raises(WorkloadError):
        host_view(dict(host(GIB), unexpected=1))  # still a closed block


def test_end_to_end_a_revised_ram_report_changes_admission_with_no_inventory_edit(authority):
    """Through the real HTTP API: the worker's report is the ONLY thing that changes."""
    worker = authority.client(W1)

    def report(available, total, boot_id):
        view = host(available, total=total, boot_id=boot_id)
        capacity = dict(cpu=8, memory_bytes=total)  # no configured capacity: the measured MemTotal
        worker.request('worker/report', dict(boot='b1', report=dict(
            capacity=capacity, available=dict(capacity), labels={}, handlers=['test.v1'], ready=True, host=view)))

    report(4*GIB, 30*GIB, BOOT_A)
    submitted = authority.client('a'*32).submit(dict(version=1, key='k', handler='test.v1',
                                                     input_digest=authority.digest,
                                                     need={'cpu': 1, 'memory_bytes': 8*GIB}))
    assert worker.request('worker/claim', {'boot': 'b1'})['assignment'] is None
    assert 'insufficient host memory' in authority.client('a'*32).get(submitted['id'])['reason']
    report(100*GIB, 123*GIB, BOOT_B)  # sticks back in, rebooted
    assert worker.request('worker/claim', {'boot': 'b1'})['assignment']['job_id'] == submitted['id']


def test_a_report_without_boot_id_is_not_judged_stale(store):
    """Mid-roll: one identity on the new worker release (sends boot_id), one on the old (does not)."""
    register(store, 'leaky-1', host(100*GIB, total=123*GIB, boot_id=None))
    register(store, 'leaky-2', host(100*GIB, total=123*GIB, boot_id=BOOT_A))
    submitted = job(store, 'mid-roll')
    assert store.claim('leaky-1', 's1')['job_id'] == submitted['id']
