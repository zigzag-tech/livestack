"""Bounded per-handler resource history and the declaration audit.

openspec/changes/measured-resource-declarations tasks 2.2-2.4. Real SQLite store; the
history is driven through `complete`, the only production writer.
"""
import sqlite3

import pytest

from livestack_node.workloads import resource_history
from livestack_node.workloads.store import WorkloadStore

GIB = 1024**3
H = 'compile.v1'


@pytest.fixture
def store(tmp_path):
    now = [1000.0]
    s = WorkloadStore(tmp_path/'authority.db', handlers={H, 'other.v1'}, clock=lambda: now[0])
    s._now = now
    return s


def run(store, handler, key, resources, outcome='succeeded', need=8*GIB):
    store.register('w', 'host', 'boot1', dict(capacity=dict(cpu=4, memory_bytes=64*GIB, disk_bytes=GIB*100),
        available=dict(cpu=4, memory_bytes=64*GIB, disk_bytes=GIB*100), labels={}, handlers=[handler], ready=True))
    store.submit('owner', dict(version=1, key=key, handler=handler, input_digest='a'*64,
                               need=dict(cpu=1, memory_bytes=need, disk_bytes=GIB)))
    a = store.claim('w', 'boot1')
    store._now[0] += 1
    return store.complete('w', 'boot1', a['attempt_id'], a['fence'], input_digest='a'*64, outcome=outcome,
                          result=dict(exit_code=0, artifacts=[], resources=resources))


def rows(store, handler=H, dimension='memory_peak'):
    with store.connect() as db:
        return [r[0] for r in db.execute('SELECT value FROM resource_history WHERE handler=? AND dimension=? '
                                         'ORDER BY at,id', (handler, dimension))]


def test_history_is_bounded_by_window_and_keeps_the_newest(store):
    for i in range(resource_history.WINDOW*10 // 5):  # 100 attempts = 2x window keeps the test quick
        run(store, H, f'k{i}', {'memory_peak_bytes': i+1})
    kept = rows(store)
    assert len(kept) == resource_history.WINDOW
    assert kept[-1] == 100 and kept[0] == 100-resource_history.WINDOW+1


def test_infrastructure_outcomes_are_not_evidence_and_a_killed_attempt_is(store):
    run(store, H, 'lost', {'memory_peak_bytes': 111}, outcome='infrastructure')
    assert rows(store) == []
    run(store, H, 'killed', {'memory_peak_bytes': 8*GIB, 'oom_kill': 1}, outcome='infrastructure')
    assert rows(store) == [8*GIB]
    with store.connect() as db:
        assert db.execute('SELECT outcome FROM resource_history').fetchone()[0] == 'resource_limit'


def test_a_missing_figure_is_absent_not_zero(store):
    run(store, H, 'none', {'resource_evidence': 'none'})
    assert rows(store) == []


def test_summary_statement_count_does_not_depend_on_handler_count(store):
    statements = []
    def count(handlers):
        with store.connect() as db:
            for i in range(handlers):
                for j in range(3):
                    resource_history.record(db, f'h{i}', f'a{handlers}-{i}-{j}', {'resources': {'memory_peak_bytes': j}},
                                            'succeeded', {'memory_bytes': 5}, 1000.0)
            statements.clear()
            db.set_trace_callback(statements.append)
            resource_history.summary(db, 1001.0)
            db.set_trace_callback(None)
        return len(statements)
    assert count(2) == count(40) == 1


@pytest.mark.parametrize('samples, observed, flagged', [
    (4, 9*GIB, False),          # below min_samples never fires
    (5, 9*GIB, True),
    (5, 8*GIB, False),          # boundary: declared == observed max is not below it
    (5, 8*GIB+1, True),         # one byte over fires
])
def test_declared_below_observed_needs_enough_samples(store, samples, observed, flagged):
    for i in range(samples):
        run(store, H, f'k{i}', {'memory_peak_bytes': observed if i == 0 else 1*GIB})
    kinds = [f['kind'] for f in store.status()['resource_audit']['flags']]
    assert ('declared_below_observed' in kinds) is flagged


def test_audit_prefers_the_nonreclaimable_peak_and_names_the_figures(store):
    for i in range(6):
        run(store, H, f'k{i}', {'memory_peak_bytes': 20*GIB, 'memory_nonreclaimable_peak_bytes': 3*GIB})
    assert store.status()['resource_audit']['flags'] == []  # 8 GiB declared > 3 GiB real; cache peak ignored
    run(store, H, 'big', {'memory_peak_bytes': 20*GIB, 'memory_nonreclaimable_peak_bytes': 9*GIB})
    flag, = [f for f in store.status()['resource_audit']['flags'] if f['kind'] == 'declared_below_observed']
    assert (flag['handler'], flag['declared'], flag['max'], flag['n']) == (H, 8*GIB, 9*GIB, 7)


def test_unreadable_history_is_stated_not_empty(store, monkeypatch):
    monkeypatch.setattr(resource_history, 'summary', lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError('x')))
    audit = store.status()['resource_audit']
    assert audit['available'] is False and audit['reason'] == 'OperationalError'


def test_backfill_rebuilds_derived_history_once_and_reports_how_many(tmp_path):
    now = [1000.0]
    s = WorkloadStore(tmp_path/'authority.db', handlers={H}, clock=lambda: now[0])
    s._now = now
    for i in range(3):
        run(s, H, f'k{i}', {'memory_peak_bytes': (i+1)*GIB})
    with s.transaction() as db:
        db.execute('DELETE FROM resource_history')
    reopened = WorkloadStore(tmp_path/'authority.db', handlers={H}, clock=lambda: now[0])
    assert reopened.resource_backfilled == 3
    assert rows(reopened) == [GIB, 2*GIB, 3*GIB]
    again = WorkloadStore(tmp_path/'authority.db', handlers={H}, clock=lambda: now[0])
    assert again.resource_backfilled == 0
