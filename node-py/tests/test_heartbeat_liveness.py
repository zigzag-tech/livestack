"""A worker that holds a valid execution lease is alive, even while its main thread sends no worker/report.

Measured 2026-10-17: a zz-joe worker that claimed a job right after a rollout enabled it spent 60+ s preparing the
attempt (input transfer, unpack, task environment), read as offline in the roster, and failed the rollout soak with
`worker_unhealthy_after_enable`. Preparation sends lease heartbeats (own connection) but no worker/report.
"""
from livestack_node.workloads import roster
from livestack_node.workloads.model import Limits
from livestack_node.workloads.store import WorkloadStore


def make(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'a.db', handlers={'test.v1'}, clock=lambda: now[0], limits=Limits())
    store.register('w1', 'h1', 'b1', dict(capacity={'cpu': 8}, available={'cpu': 8}, labels={'os': 'linux'},
                                          handlers=['test.v1'], ready=True))
    store.submit('o', dict(version=1, key='k', handler='test.v1', input_digest='a'*64, need={'cpu': 1}))
    return store, now


def worker_entry(store):
    return next(e for e in roster.build(store, [])['workers'] if e['id'] == 'w1')


def test_a_worker_preparing_an_attempt_stays_connected_through_lease_heartbeats(tmp_path):
    store, now = make(tmp_path)
    a = store.claim('w1', 'b1')
    for _ in range(12):  # two minutes of preparation: twice fresh_seconds, heartbeats only
        now[0] += 10
        store.heartbeat(a['worker'], a['boot'], a['attempt_id'], a['fence'])
    entry = worker_entry(store)
    assert now[0] - 1000 > 2*store.limits.fresh_seconds - 1
    assert entry['connected'] and entry['state'] == 'running' and entry['last_seen_age_s'] < store.limits.fresh_seconds


def test_failure_control_a_worker_with_no_heartbeat_and_no_report_still_goes_offline(tmp_path):
    store, now = make(tmp_path)
    store.claim('w1', 'b1')
    now[0] += store.limits.fresh_seconds + 1
    assert not worker_entry(store)['connected'], 'silence must still read as silence'


def test_a_heartbeat_moves_seen_only_not_the_reported_capacity(tmp_path):
    store, now = make(tmp_path)
    a = store.claim('w1', 'b1')
    before = worker_entry(store)['capacity']
    now[0] += 30
    store.heartbeat(a['worker'], a['boot'], a['attempt_id'], a['fence'])
    assert worker_entry(store)['capacity'] == before
