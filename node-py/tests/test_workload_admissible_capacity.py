"""`handlers/<h>/capacity` answers admissible concurrency, not seats (workloads/capacity.py).

2026-11-19: node-g offered 7 idle e2e seats while its model server held ~57 of 60 GB at peak; placement
admitted none of them, and ZZOPS sized its in-flight bound from `ready_workers` = 7. Real SQLite store and
real placement throughout: every figure below is what `claim` would actually do.
"""
from livestack_node.workloads.store import WorkloadStore

GIB = 1024**3
E2E = 'benchday.e2e.full.v1'
IDLE = 'idle.v1'


def host(available, total, services=None):
    window = dict(some_avg10=0.0, some_avg60=0.0, full_avg10=0.0, full_avg60=0.5)
    return dict(memory_total_bytes=total, memory_available_bytes=available, memory_reserve_bytes=GIB,
                swap_in_bytes_per_second=0.0, psi=dict(memory=window, io=window, cpu=window),
                attempts={}, services=services or {})


def register(store, worker, hostname, view, handlers=(E2E,)):
    capacity = dict(cpu=32, memory_bytes=view['memory_total_bytes'], disk_bytes=200*GIB)
    report = dict(capacity=capacity, available=dict(capacity), labels={'os': 'linux'}, handlers=list(handlers),
                  ready=True, host=view)
    store.register(worker, hostname, 's1', report)


def job(store, key):
    return store.submit('owner', dict(version=1, key=key, handler=E2E, input_digest='a'*64,
                                      need=dict(cpu=2, memory_bytes=10*GIB, disk_bytes=GIB)))


def fleet(tmp_path, clock):
    store = WorkloadStore(tmp_path/'authority.db', handlers={E2E, IDLE}, clock=lambda: clock[0])
    # Seven seats beside a model server whose unrealised peak (57 - 40 GiB) is still to come:
    # 20 available - 1 reserve - 17 = 2 GiB free, below one 10 GiB claim.
    strata = {'harmony-llm': dict(peak_bytes=57*GIB, current_bytes=40*GIB, resident=False)}
    for i in range(7):
        register(store, f'node_g-{i}', 'node-g', host(20*GIB, 60*GIB, strata))
    # Three seats sharing 25 GiB available: 24 free admits two 10 GiB jobs, not three.
    for i in range(3):
        register(store, f'joe-{i}', 'node-f', host(25*GIB, 64*GIB))
    # Plenty: both seats admit.
    for i in range(2):
        register(store, f'mac-{i}', 'node-b', host(100*GIB, 128*GIB))
    return store


def by_host(answer):
    return {h['host']: h for h in answer['hosts']}


def test_admissible_follows_host_memory_not_seats(tmp_path):
    clock = [1000.0]
    store = fleet(tmp_path, clock)
    job(store, 'template')
    answer = store.handler_capacity(E2E)
    assert answer['ready_workers'] == 12  # seats: unchanged meaning
    hosts = by_host(answer)
    assert hosts['node-g']['seats'] == 7 and hosts['node-g']['admissible'] == 0
    assert hosts['node-g']['refused'] == {'memory_insufficient': 7}
    assert hosts['node-f']['admissible'] == 2 and hosts['node-f']['refused'] == {'memory_insufficient': 1}
    assert hosts['node-b']['admissible'] == 2
    assert answer['admissible'] == 4
    assert answer['charge']['memory_claim_bytes'] == 10*GIB and 'latest' in answer['basis']


def test_admissible_agrees_with_real_claims(tmp_path):
    """The positive control: place jobs for real; exactly `admissible` of them start."""
    clock = [1000.0]
    store = fleet(tmp_path, clock)
    for i in range(12):
        job(store, f'j{i}')
    predicted = store.handler_capacity(E2E)['admissible']
    started = sum(1 for w in [f'node_g-{i}' for i in range(7)] + [f'joe-{i}' for i in range(3)] +
                  ['mac-0', 'mac-1'] if store.claim(w, 's1'))
    assert started == predicted == 4
    # Running attempts of the handler still count: the bound must not shrink under its own jobs.
    after = store.handler_capacity(E2E)
    assert after['admissible'] == 4
    assert by_host(after)['node-f']['running'] == 2


def test_offline_and_drained_workers_are_not_counted(tmp_path):
    clock = [1000.0]
    store = fleet(tmp_path, clock)
    job(store, 'template')
    answer = store.handler_capacity(E2E, draining={'mac-0'})
    assert answer['ready_workers'] == 11 and by_host(answer)['node-b']['seats'] == 1
    assert answer['admissible'] == 3
    clock[0] += 10_000  # every report stale: offline
    register(store, 'mac-1', 'node-b', host(100*GIB, 128*GIB))
    answer = store.handler_capacity(E2E)
    assert answer['ready_workers'] == 1 and answer['admissible'] == 1
    assert [h['host'] for h in answer['hosts']] == ['node-b']


def test_handler_with_no_declared_need_counts_idle_eligible_seats(tmp_path):
    clock = [1000.0]
    store = fleet(tmp_path, clock)
    register(store, 'node_g-idle', 'node-g', host(20*GIB, 60*GIB), handlers=(E2E, IDLE))
    answer = store.handler_capacity(IDLE)
    assert answer['ready_workers'] == 1 and answer['admissible'] == 1
    assert 'no declared need' in answer['basis']
    assert answer['charge'] == dict(admit={}, memory_claim_bytes=None)
