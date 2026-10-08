"""Typed terminal causes and structured placement blockers (openspec typed-outcome-causes-and-blockers)."""
import json
import random
import sqlite3

import pytest

from livestack_node.workloads import causes, placement
from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.store import WorkloadStore


def req(key, **extra):
    version = 4 if {'scope', 'max_queue_seconds', 'progress_deadline_seconds'} & set(extra) else 1
    return dict(version=version, key=key, handler='test.v1', input_digest='a'*64, need={'cpu': 1}, **extra)


def register(store, worker='w1', host='h1', boot='b1', labels=None, handlers=('test.v1',)):
    return store.register(worker, host, boot, dict(capacity={'cpu': 8}, available={'cpu': 8},
                                                    labels=labels or {'os': 'linux'}, handlers=list(handlers), ready=True))


def finish(store, a, outcome='succeeded', result=None):
    return store.complete(a['worker'], a['boot'], a['attempt_id'], a['fence'], input_digest='a'*64,
                          outcome=outcome, result=result or {'exit_code': 0})


@pytest.fixture
def harness(tmp_path):
    now = [1000.0]
    return WorkloadStore(tmp_path/'a.db', handlers={'test.v1', 'other.v1'}, clock=lambda: now[0]), now, tmp_path/'a.db'


# ---- derivation: one case per kind the authority can name -------------------------------------------------

@pytest.mark.parametrize('outcome,result,kind,retry', [
    ('succeeded', {'exit_code': 0}, 'succeeded', 'no'),
    ('product_failure', {'exit_code': 2}, 'handler_failed', 'no'),
    ('infrastructure', {'error': 'WorkloadError', 'detail': 'execution lease lost', 'resources': {}}, 'lease_lost', 'elsewhere'),
    ('infrastructure', {'error': 'abandoned', 'detail': 'execution lease expired'}, 'worker_lost', 'elsewhere'),
    ('infrastructure', {'error': 'WorkloadError', 'detail': 'x', 'resources': {'unit_result': 'timeout'}}, 'wall_time_exceeded', 'after_change'),
    ('infrastructure', {'cause_kind': 'capability_absent'}, 'capability_absent', 'elsewhere'),
    ('infrastructure', {'cause_kind': 'brand_new_kind_from_a_newer_worker'}, 'unknown', 'elsewhere'),
    ('infrastructure', {'error': 'WorkloadError', 'detail': 'execution stopped without a result',
                        'resources': {'resource_evidence': 'none'}}, 'unknown', 'elsewhere'),
])
def test_derivation_table(outcome, result, kind, retry):
    cause = causes.derive(outcome, result, fence=1, attempts=2)
    assert (cause['kind'], cause['retry']) == (kind, retry)
    assert causes.validate_stored(cause)


def test_unreadable_evidence_is_never_guessed_to_be_an_oom_kill():
    cause = causes.derive('infrastructure', {'error': 'WorkloadError', 'detail': 'execution stopped without a result',
                                             'resources': {'resource_evidence': 'none'}}, fence=1, attempts=2)
    assert cause['kind'] == 'unknown'
    assert 'memory.events' in cause['evidence']['unreadable'] and 'unit properties' in cause['evidence']['unreadable']
    assert cause['evidence']['detail'] == 'execution stopped without a result'
    assert causes.derive('infrastructure', {}, fence=2, attempts=2)['retry'] == 'no', 'advice stops with the attempts'


def test_unknown_reported_kind_keeps_its_name_bounded():
    cause = causes.derive('infrastructure', {'cause_kind': 'x'*500})
    assert cause['kind'] == 'unknown' and len(cause['evidence']['reported_kind']) == 40


def test_evidence_is_bounded():
    cause = causes.make('unknown', {'a': 'x'*5000, 'b': 'y'})
    assert len(json.dumps(cause)) <= causes.CAUSE_BYTES + 100 and cause['evidence'].get('truncated') is True


def test_classifier_fault_is_unknown_not_a_lost_completion():
    class Hostile(dict):
        def get(self, *a):
            raise RuntimeError('boom')
    cause = causes.derive('infrastructure', Hostile(), fence=1, attempts=2)
    assert cause['kind'] == 'unknown' and 'classifier_error' in cause['evidence']


# ---- the store stores one on every terminal path ---------------------------------------------------------

def test_every_terminal_path_stores_a_cause(harness):
    store, now, _ = harness
    register(store)
    seen = {}
    ok = store.submit('o', req('ok')); a = store.claim('w1', 'b1'); finish(store, a)
    seen['succeeded'] = store.get('o', ok['id'])
    bad = store.submit('o', req('bad')); a = store.claim('w1', 'b1'); finish(store, a, 'product_failure', {'exit_code': 3})
    seen['handler_failed'] = store.get('o', bad['id'])
    oom = store.submit('o', req('oom')); a = store.claim('w1', 'b1')
    finish(store, a, 'infrastructure', {'error': 'WorkloadError', 'detail': 'execution lease lost',
                                        'resources': {'oom_kill': 1, 'memory_peak_bytes': 9, 'source': 'unit'}})
    seen['oom_killed'] = store.get('o', oom['id'])
    cancelled = store.submit('o', req('c')); store.cancel('o', cancelled['id'])
    seen['cancelled_by_owner'] = store.get('o', cancelled['id'])
    withdrawn = store.submit('o', req('w')); store.withdraw('o', withdrawn['id'])
    assert store.get('o', withdrawn['id'])['cause']['evidence'] == {'withdrawn': True}
    scoped = store.submit('o', dict(req('s', scope={'key': 'sc'}))); store.close_scope('o', 'sc', 'why')
    seen['scope_closed'] = store.get('o', scoped['id'])
    late = store.submit('o', req('d', deadline=now[0]+10)); now[0] += 20
    seen['deadline_expired'] = store.get('o', late['id'])
    lost = store.submit('o', req('l')); a = store.claim('w1', 'b1'); now[0] += 1000
    seen['_lost'] = store.get('o', lost['id'])  # first attempt abandoned: re-queued, no terminal cause yet
    assert seen['_lost']['state'] == 'queued' and seen['_lost']['cause'] is None
    for kind, job in seen.items():
        if kind.startswith('_'):
            continue
        assert job['cause']['kind'] == kind, (kind, job['cause'])
        assert causes.validate_stored(job['cause'])
    assert seen['oom_killed']['cause']['retry'] == 'after_change'
    assert seen['oom_killed']['cause']['evidence']['observed'] == 9
    assert seen['scope_closed']['cause']['evidence']['scope'] == 'sc'
    assert seen['succeeded']['cause_reason'] if 'cause_reason' in seen['succeeded'] else True


def test_worker_lost_after_the_last_attempt(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'a.db', handlers={'test.v1'}, clock=lambda: now[0], limits=Limits(attempts=1))
    register(store)
    job = store.submit('o', req('l')); store.claim('w1', 'b1'); now[0] += 1000
    done = store.get('o', job['id'])
    assert done['state'] == 'failed' and done['cause']['kind'] == 'worker_lost'
    assert done['cause']['evidence']['worker'] == 'w1'


def test_rows_from_before_causes_say_so_rather_than_unknown(harness):
    store, _, path = harness
    job = store.submit('o', req('old')); store.cancel('o', job['id'])
    with sqlite3.connect(path) as db:
        db.execute("UPDATE jobs SET cause=NULL")
    old = store.get('o', job['id'])
    assert old['cause'] is None and old['cause_reason'] == 'predates_causes'
    queued = store.submit('o', req('new'))
    assert queued['cause'] is None and 'cause_reason' not in queued


def test_migration_of_a_database_that_predates_causes(tmp_path):
    path = tmp_path/'old.db'
    WorkloadStore(path, handlers={'test.v1'}).submit('o', req('a'))
    with sqlite3.connect(path) as db:
        for table, column in (('jobs', 'cause'), ('jobs', 'placement'), ('attempts', 'progress_changed')):
            db.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
    store = WorkloadStore(path, handlers={'test.v1'})
    job = store.submit('o', req('b')); store.cancel('o', job['id'])
    assert store.get('o', job['id'])['cause']['kind'] == 'cancelled_by_owner'


# ---- opt-in deadlines ---------------------------------------------------------------------------------------

def test_queue_wait_deadline_is_opt_in_and_names_the_last_blockers(harness):
    store, now, _ = harness
    plain = store.submit('o', req('plain'))
    waiting = store.submit('o', req('wait', max_queue_seconds=60))
    register(store, handlers=('other.v1',))
    store.claim('w1', 'b1')  # a placement round records the blocker
    now[0] += 59
    assert store.get('o', waiting['id'])['state'] == 'queued'
    now[0] += 2
    done = store.get('o', waiting['id'])
    assert done['state'] == 'expired' and done['cause']['kind'] == 'unplaceable'
    assert done['cause']['evidence']['max_queue_seconds'] == 60
    assert store.get('o', plain['id'])['state'] == 'queued', 'no deadline unless asked'


def test_queue_wait_deadline_does_not_touch_a_job_that_was_attempted(harness):
    store, now, _ = harness
    register(store)
    job = store.submit('o', req('k', max_queue_seconds=30)); store.claim('w1', 'b1')
    now[0] += 31
    assert store.get('o', job['id'])['state'] == 'running'


def test_progress_deadline_ends_a_stalled_attempt_and_changed_progress_resets_it(harness):
    store, now, _ = harness
    register(store)
    job = store.submit('o', req('k', progress_deadline_seconds=100))
    a = store.claim('w1', 'b1')
    now[0] += 90
    store.heartbeat('w1', 'b1', a['attempt_id'], a['fence'], progress={'phase': 'compile', 'fraction': 0.1})
    now[0] += 90
    store.heartbeat('w1', 'b1', a['attempt_id'], a['fence'], progress={'phase': 'compile', 'fraction': 0.1})
    assert store.get('o', job['id'])['state'] == 'running', 'moved 90s ago'
    now[0] += 20
    with pytest.raises(WorkloadError, match='no longer valid'):
        store.heartbeat('w1', 'b1', a['attempt_id'], a['fence'], progress={'phase': 'compile', 'fraction': 0.1})
    done = store.get('o', job['id'])
    assert done['state'] == 'failed' and done['cause']['kind'] == 'stalled_no_progress'
    assert done['attempts'][0]['state'] == 'cleanup', 'the worker is held until clean, as for any fenced attempt'


def test_deadline_fields_need_schema_four_and_a_sane_range(harness):
    store, _, _ = harness
    with pytest.raises(WorkloadError):
        store.submit('o', dict(req('x'), max_queue_seconds=60))
    for bad in (5, 100000, True, 'x'):
        with pytest.raises(WorkloadError):
            store.submit('o', dict(req('y'), version=4, progress_deadline_seconds=bad))


# ---- placement blockers -----------------------------------------------------------------------------------

def blockers(store, owner, job):
    placement_doc = store.get(owner, job['id'])['placement']
    return {b['code'] for b in placement_doc['blockers']} if placement_doc else None


def test_handler_not_advertised_then_capability_absent(harness):
    store, now, _ = harness
    job = store.submit('o', req('k', selector={'gpu': 'yes'}))
    register(store, handlers=('other.v1',))
    store.claim('w1', 'b1')
    assert blockers(store, 'o', job) == {'handler_not_advertised'}
    register(store)
    store.claim('w1', 'b1')
    assert blockers(store, 'o', job) == {'capability_absent'}
    shown = store.get('o', job['id'])['placement']['blockers'][0]
    assert shown['worker'] == 'w1' and shown['host'] == 'h1' and len(shown['detail']) <= 160


def test_busy_worker_blocker_and_cleared_on_claim(harness):
    store, now, _ = harness
    register(store)
    first = store.submit('o', req('one')); second = store.submit('o', req('two'))
    a = store.claim('w1', 'b1')
    store.claim('w1', 'b1')
    assert blockers(store, 'o', second) == {'worker_busy'}
    finish(store, a)
    assert store.claim('w1', 'b1')
    assert store.get('o', second['id'])['placement'] is None


def test_steady_wait_writes_the_job_row_once_and_since_is_stable(tmp_path):
    now = [1000.0]
    statements = []

    class Counting(WorkloadStore):
        def connect(self):
            db = super().connect()
            db.set_trace_callback(lambda sql: statements.append(sql) if sql.startswith('UPDATE jobs SET') else None)
            return db

    store = Counting(tmp_path/'a.db', handlers={'test.v1', 'other.v1'}, clock=lambda: now[0])
    job = store.submit('o', req('k', selector={'gpu': 'yes'}))
    register(store)
    store.claim('w1', 'b1')
    since = store.get('o', job['id'])['placement']['since']
    del statements[:]
    for _ in range(20):
        now[0] += 1
        store.claim('w1', 'b1')
    assert statements == [], statements[:3]
    now[0] += 61
    register(store)
    store.claim('w1', 'b1')
    refreshed = store.get('o', job['id'])['placement']
    assert refreshed['since'] == since and refreshed['evaluated'] > since
    assert len(statements) == 1, 'one refresh per minute, not one per poll'


def test_positive_control_old_behaviour_wrote_every_round(tmp_path, monkeypatch):
    now = [1000.0]
    statements = []

    def old_wait(db, row, when, reason, blocks):  # the pre-change behaviour: overwrite reason every round
        db.execute("UPDATE jobs SET reason=? WHERE id=?", (reason[:8192], row['id']))
    monkeypatch.setattr(placement, '_wait', old_wait)

    class Counting(WorkloadStore):
        def connect(self):
            db = super().connect()
            db.set_trace_callback(lambda sql: statements.append(sql) if sql.startswith('UPDATE jobs SET reason') else None)
            return db
    store = Counting(tmp_path/'a.db', handlers={'test.v1', 'other.v1'}, clock=lambda: now[0])
    store.submit('o', req('k', selector={'gpu': 'yes'}))
    register(store)
    store.claim('w1', 'b1')
    del statements[:]
    for _ in range(20):
        now[0] += 1
        store.claim('w1', 'b1')
    assert len(statements) == 20, 'the instrument detects the old per-round write'


def test_blocker_document_is_bounded(harness):
    store, now, _ = harness
    job = store.submit('o', req('k', selector={'gpu': 'yes'}))
    for n in range(30):
        register(store, worker=f'w{n}', host=f'h{n}', boot='b')
    store.claim('w0', 'b')
    doc = store.get('o', job['id'])['placement']
    assert len(doc['blockers']) == 16 and doc['truncated'] is True
    with sqlite3.connect(harness[2]) as db:
        assert len(db.execute('SELECT placement FROM jobs').fetchone()[0].encode()) <= placement.PLACEMENT_BYTES


# ---- seeded lifecycle property: a terminal job always carries a consistent cause ---------------------------

def lifecycle(seed, store_factory, tmp):
    rnd = random.Random(seed)
    now = [1000.0]
    store = store_factory(tmp/f'p{seed}.db', handlers={'test.v1'}, clock=lambda: now[0])
    register(store)
    claimed, jobs = [], []
    for step in range(40):
        op = rnd.choice(('submit', 'submit', 'claim', 'done', 'cancel', 'withdraw', 'advance', 'close'))
        try:
            if op == 'submit':
                extra = rnd.choice(({}, {'deadline': now[0] + rnd.choice((5, 500))}, {'scope': {'key': rnd.choice('ab')}},
                                    {'max_queue_seconds': 30}, {'progress_deadline_seconds': 40}))
                jobs.append(store.submit('o', req('k%d' % step, **extra))['id'])
            elif op == 'claim':
                a = store.claim('w1', 'b1')
                a and claimed.append(a)
            elif op == 'done' and claimed:
                a = claimed.pop(rnd.randrange(len(claimed)))
                outcome = rnd.choice(('succeeded', 'product_failure', 'infrastructure'))
                finish(store, a, outcome, {'exit_code': 0 if outcome == 'succeeded' else 1,
                                           **({'resources': {'oom_kill': 1}} if rnd.random() < .3 else {})})
            elif op == 'cancel' and jobs:
                store.cancel('o', rnd.choice(jobs))
            elif op == 'withdraw' and jobs:
                store.withdraw('o', rnd.choice(jobs))
            elif op == 'close':
                store.close_scope('o', rnd.choice('ab'), 'x')
            elif op == 'advance':
                now[0] += rnd.choice((10, 50, 500))
        except WorkloadError:
            pass
        for jid in jobs:
            job = store.get('o', jid)
            if job['state'] in ('succeeded', 'failed', 'cancelled', 'expired'):
                cause = job['cause']
                assert cause is not None and causes.validate_stored(cause), (seed, step, job['state'], cause)
                outcome = (job['result'] or {}).get('outcome')
                if outcome == 'infrastructure':
                    assert cause['kind'] != 'handler_failed', (seed, step, cause)
                if outcome == 'product_failure':
                    assert cause['kind'] == 'handler_failed', (seed, step, cause)
            else:
                assert job['cause'] is None, (seed, step, job['state'], job['cause'])


class NoCauseOnCancel(WorkloadStore):
    """Mutant: the shared cancel path forgets to record why."""
    def _cancel_job(self, db, job_id, reason, now, cause):
        super()._cancel_job(db, job_id, reason, now, cause)
        db.execute("UPDATE jobs SET cause=NULL WHERE id=?", (job_id,))


def test_terminal_jobs_always_carry_a_consistent_cause(tmp_path):
    for seed in range(40):
        lifecycle(seed, WorkloadStore, tmp_path)


def test_mutant_that_drops_the_cancel_cause_is_caught(tmp_path):
    with pytest.raises(AssertionError):
        for seed in range(40):
            lifecycle(seed, NoCauseOnCancel, tmp_path)


# ---- worker evidence -> cause, from `systemctl show` output recorded off real units ---------------------------

def test_recorded_systemd_properties_classify_without_a_receipt():
    from livestack_node.workloads.resource_usage import merge_evidence, unit_evidence
    timeout = {'Result': 'timeout', 'ExecMainStatus': '0', 'OOMKills': '0', 'MemoryPeak': '123456',
               'CPUUsageNSec': '2000000000', 'ActiveState': 'failed'}
    resources = merge_evidence(None, {'cpu_usage_usec': 5}, unit_evidence(timeout))
    assert resources['unit_result'] == 'timeout' and resources['exec_main_status'] == 0
    cause = causes.derive('infrastructure', {'error': 'WorkloadError', 'detail': 'execution stopped without a result',
                                             'resources': resources}, fence=1, attempts=2)
    assert cause['kind'] == 'wall_time_exceeded' and cause['retry'] == 'after_change'
    clean = unit_evidence({'Result': 'success', 'ExecMainStatus': '0', 'OOMKills': '[not set]'})
    assert 'unit_result' not in clean and 'oom_kill' not in clean
    oom = merge_evidence(None, {}, unit_evidence({'Result': 'oom-kill', 'OOMKills': '1', 'MemoryPeak': '8589934592'}))
    assert oom['oom_kill'] == 1 and oom['unit_result'] == 'oom-kill' and oom['source'] == 'unit'
