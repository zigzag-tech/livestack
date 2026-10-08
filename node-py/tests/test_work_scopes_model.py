"""Seeded model-based property test for work scopes, with mutants that prove the instrument can fail.

Random sequences of submit / claim / complete / heartbeat / close / renew / clock advance / authority restart
are driven against a REAL WorkloadStore on a real SQLite file. After every step the invariants S1..S6 of
openspec work-scopes-and-cascade-cancel (design section 8) are checked by reading the database directly.
Sequence count: WORK_SCOPE_SEQUENCES (default 60; the proposal's full run is 2000).
"""
import os
import random
import sqlite3

import pytest

from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.store import WorkloadStore

SEQUENCES = int(os.environ.get('WORK_SCOPE_SEQUENCES', '60'))
STEPS = 40
OWNERS = ('o', 'p')
SCOPES = ('s1', 's2', 's3')
KEYS = tuple('k%d' % i for i in range(10))


def request(key, scope, lease):
    body = dict(version=4 if scope else 1, key=key, handler='test.v1', input_digest='a'*64, need={'cpu': 1})
    if scope:
        body['scope'] = {'key': scope, 'lease_seconds': lease}
    return body


class NoCascade(WorkloadStore):
    """Mutant: closing a scope marks it closed but leaves its jobs running."""
    def _close_scope(self, *args, **kwargs):
        self._cancel_job = lambda *a, **k: None
        try:
            return super()._close_scope(*args, **kwargs)
        finally:
            del self._cancel_job


class NoLeaseJanitor(WorkloadStore):
    """Mutant: a lapsed lease is never acted on."""
    def _expire(self, db, now):
        real = db.execute

        class Proxy:
            def __getattr__(self, attribute):
                return getattr(db, attribute)

            def execute(self, sql, *args):
                if 'FROM scopes WHERE state=\'open\' AND lease_expires' in sql:
                    return db.execute('SELECT owner,key FROM scopes WHERE 0')
                return real(sql, *args)
        return super()._expire(Proxy(), now)


def rows(path, sql, *args):
    with sqlite3.connect(path) as db:
        db.row_factory = sqlite3.Row
        return [dict(r) for r in db.execute(sql, args)]


def snapshot(path):
    return {t: rows(path, 'SELECT * FROM %s ORDER BY 1,2' % t) for t in ('jobs', 'attempts', 'workers', 'scopes')}


def check(path, now, label):
    # S1: nothing live in a closed scope.
    live = rows(path, "SELECT j.id FROM jobs j JOIN scopes s ON s.owner=j.owner AND s.key=j.scope "
                      "WHERE s.state='closed' AND j.state IN ('queued','running')")
    assert not live, '%s: S1 jobs still live in a closed scope: %s' % (label, live)
    # S5: no lapsed open lease survives a store transaction (at most 6 scopes here, under the per-call bound).
    lapsed = rows(path, "SELECT owner,key FROM scopes WHERE state='open' AND lease_expires<=?", now)
    assert not lapsed, '%s: S5 lapsed lease left open: %s' % (label, lapsed)


def run(seed, factory, tmp):
    rnd = random.Random(seed)
    path = tmp / ('m%d.db' % seed)
    clock = [1000.0]
    store = factory(path, handlers={'test.v1'}, clock=lambda: clock[0], limits=Limits(attempts=2))
    store.register('w1', 'h1', 'b1', dict(capacity={'cpu': 4}, available={'cpu': 4}, labels={'os': 'linux'},
                                           handlers=['test.v1'], ready=True))
    claimed = []
    for step in range(STEPS):
        op = rnd.choice(('submit', 'submit', 'submit', 'claim', 'complete', 'heartbeat', 'close', 'close', 'renew',
                         'advance', 'advance', 'restart', 'register'))
        label = 'seed %d step %d %s' % (seed, step, op)
        owner = rnd.choice(OWNERS)
        scope = rnd.choice(SCOPES)
        try:
            if op == 'submit':
                key, use_scope = rnd.choice(KEYS), rnd.random() < 0.85
                state = rows(path, "SELECT state,lease_expires FROM scopes WHERE owner=? AND key=?", owner, scope)
                closed = bool(state) and (state[0]['state'] == 'closed' or
                                         state[0]['lease_expires'] is not None and state[0]['lease_expires'] <= clock[0])
                before = rows(path, 'SELECT count(*) AS n FROM jobs')[0]['n']
                existing = rows(path, "SELECT scope FROM jobs WHERE owner=? AND request_key=?", owner, key)
                try:
                    store.submit(owner, request(key, scope if use_scope else None, rnd.choice((300, 600, 1800))))
                    # S2: a closed scope never yields a job (new or replayed).
                    assert not (use_scope and closed), '%s: S2 submit into closed scope %s succeeded' % (label, scope)
                except WorkloadError as error:
                    if use_scope and closed and not existing:
                        assert error.status == 409 and 'scope_closed' in str(error), '%s: %s' % (label, error)
                    elif not (error.status in (409, 429)):
                        raise
                if use_scope and closed:
                    assert rows(path, 'SELECT count(*) AS n FROM jobs')[0]['n'] == before, '%s: S2 job created' % label
            elif op == 'claim':
                got = store.claim('w1', 'b1')
                if got:
                    claimed.append(got)
            elif op == 'complete' and claimed:
                a = claimed.pop(rnd.randrange(len(claimed)))
                try:
                    store.complete(a['worker'], a['boot'], a['attempt_id'], a['fence'], input_digest='a'*64,
                                   outcome='succeeded', result={'exit': 0})
                except WorkloadError:
                    pass  # fenced: the scope was closed under it, which is the point
            elif op == 'heartbeat' and claimed:
                a = rnd.choice(claimed)
                try:
                    store.heartbeat(a['worker'], a['boot'], a['attempt_id'], a['fence'])
                except WorkloadError:
                    pass
            elif op == 'close':
                terminal_before = rows(path, "SELECT * FROM jobs WHERE state IN ('succeeded','failed','cancelled','expired') ORDER BY id")
                store.close_scope(owner, scope, 'model')
                terminal_after = {r['id']: r for r in rows(path, "SELECT * FROM jobs ORDER BY id")}
                for row in terminal_before:  # S4
                    assert terminal_after.get(row['id']) == row, '%s: S4 terminal job changed by close' % label
                again = snapshot(path)
                second = store.close_scope(owner, scope, 'model again')
                assert second['replayed'], '%s: S3 second close not a replay' % label
                assert snapshot(path) == again, '%s: S3 second close changed rows' % label
            elif op == 'renew':
                try:
                    store.renew_scope(owner, scope)
                except WorkloadError as error:
                    assert error.status in (404, 409), error
            elif op == 'advance':
                clock[0] += rnd.choice((1, 60, 301, 900, 2000))
            elif op == 'restart':
                store = factory(path, handlers={'test.v1'}, clock=lambda: clock[0], limits=Limits(attempts=2))
                store.recover()
            elif op == 'register':
                store.register('w1', 'h1', 'b1', dict(capacity={'cpu': 4}, available={'cpu': 4}, labels={'os': 'linux'},
                                                       handlers=['test.v1'], ready=True),
                               cleaned=[a['attempt_id'] for a in claimed][:0])
        except WorkloadError:
            pass
        store.sweep()  # the "next transaction" of S5
        check(path, clock[0], label)
        for scope_row in rows(path, 'SELECT owner,key FROM scopes'):  # S6
            view = store.get_scope(scope_row['owner'], scope_row['key'])
            recount = {r['state']: r['n'] for r in rows(
                path, 'SELECT state,count(*) AS n FROM jobs WHERE owner=? AND scope=? GROUP BY state',
                scope_row['owner'], scope_row['key'])}
            assert {k: v for k, v in view['jobs'].items() if v} == recount, '%s: S6 counts differ' % label


def first_violation(factory, tmp, seeds):
    for seed in seeds:
        try:
            run(seed, factory, tmp)
        except AssertionError as error:
            return str(error)
    return None


def test_invariants_hold_over_random_sequences(tmp_path):
    shm = tmp_path
    assert first_violation(WorkloadStore, shm, range(SEQUENCES)) is None


def test_mutant_without_cascade_is_caught_by_s1(tmp_path):
    message = first_violation(NoCascade, tmp_path, range(SEQUENCES))
    assert message is not None and 'S1' in message, message


def test_mutant_without_lease_janitor_is_caught_by_s5(tmp_path):
    message = first_violation(NoLeaseJanitor, tmp_path, range(SEQUENCES))
    assert message is not None and 'S5' in message, message
