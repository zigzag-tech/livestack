"""Claims (drain/enable) with owner, mandatory expiry and compare-and-swap, against a REAL authority.

openspec/changes/declarative-worker-rollout, tasks 1.1-1.5. Every test drives the HTTP API of a real
WorkloadServer over SQLite; none uses a fake store.
"""
import hashlib
import json
import sqlite3
from io import BytesIO
from threading import Thread

import pytest

from livestack_node.workloads import claims as claims_module
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.reload_status import file_hash
from livestack_node.workloads.service import reload_principals
from livestack_node.workloads.store import WorkloadStore

A, ADMIN, ROLL, W1, W2 = 'a'*32, 'm'*32, 'r'*32, 'w'*32, 'x'*32
REPORT = dict(boot='b1', report=dict(capacity={'cpu': 2}, available={'cpu': 2}, labels={},
                                     handlers=['test.v1'], ready=True))


def principals(extra=(), workers=(('w1', W1, 'h1'), ('w2', W2, 'h2'))):
    out = [dict(id='alice', token=A, role='caller', handlers=['test.v1']),
           dict(id='ops', token=ADMIN, role='admin', handlers=['test.v1']),
           dict(id='reconciler', token=ROLL, role='rollout')]
    out += [dict(id=i, token=t, role='worker', worker=i, host=h) for i, t, h in workers]
    return out + list(extra)


class Authority:
    def __init__(self, tmp_path, plist=None):
        self.config = tmp_path/'authority.json'
        self.write(plist or principals())
        self.now = [1000.0]
        self.store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: self.now[0])
        self.server = WorkloadServer(('127.0.0.1', 0), self.store,
                                     [Principal(**p) for p in json.loads(self.config.read_text())['principals']])
        self.server.claims.sync_file(self.server.principals)
        Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        data = b'input'
        self.digest = hashlib.sha256(data).hexdigest()
        self.server.blobs.put('alice', self.digest, len(data), BytesIO(data))

    def write(self, plist):
        self.config.write_text(json.dumps(dict(principals=plist, handlers=['test.v1'])))

    def reload(self):
        return reload_principals(self.server, self.config, attempts=1, pause=0)

    def client(self, token):
        return WorkloadClient(self.url, token, timeout=5)

    def call(self, token, route, body=None):
        return self.client(token).request(route, body)

    def refused(self, token, route, body=None):
        with pytest.raises(WorkloadError) as error:
            self.call(token, route, body)
        return error.value

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def authority(tmp_path):
    made = Authority(tmp_path)
    yield made
    made.close()


def drain(authority, token=ADMIN, worker='w1', **body):
    body.setdefault('ttl_seconds', 600)
    return authority.call(token, f'claims/{worker}/drain', body)


def test_drain_without_expiry_is_refused_by_name(authority):
    error = authority.refused(ADMIN, 'claims/w1/drain', {'owner': 'x', 'reason': 'roll'})
    assert error.status == 400 and 'drain_requires_expiry' in str(error)
    assert authority.call(ADMIN, 'claims')['claims'][0]['enabled'] is True  # nothing was written


def test_ttl_cap_and_until_forms(authority):
    error = authority.refused(ADMIN, 'claims/w1/drain', {'ttl_seconds': 25*3600})
    assert 'drain_ttl_exceeds_cap' in str(error)
    claim = authority.call(ADMIN, 'claims/w1/drain', {'until': '1970-01-01T00:30:00Z'})
    assert claim['expires_at'] == 1800.0 and claim['generation'] == 2  # import was generation 1
    assert authority.refused(ADMIN, 'claims/w2/drain', {'until': 5}).status == 400  # in the past


def test_drain_blocks_new_claims_but_not_the_running_attempt(authority):
    worker = authority.client(W1)
    worker.request('worker/report', REPORT)
    job = authority.client(A).submit(dict(version=1, key='k', handler='test.v1',
                                          input_digest=authority.digest, need={'cpu': 1}))
    attempt = worker.request('worker/claim', {'boot': 'b1'})['assignment']
    assert attempt['job_id'] == job['id']
    second = authority.client(A).submit(dict(version=1, key='k2', handler='test.v1',
                                             input_digest=authority.digest, need={'cpu': 1}))
    drain(authority, owner='agent-1', reason='roll')
    assert worker.request('worker/claim', {'boot': 'b1'}) == {'assignment': None, 'reason': 'worker_draining'}
    identity = dict(boot='b1', attempt_id=attempt['attempt_id'], fence=attempt['fence'])
    assert worker.request('worker/heartbeat', identity)['lease_remaining'] > 0  # existing attempt keeps access
    assert authority.client(A).get(second['id'])['state'] == 'queued'
    authority.call(ADMIN, 'claims/w1/enable', {'owner': 'agent-1'})
    assert worker.request('worker/claim', {'boot': 'b1'})  # claims work again


def test_second_owner_is_refused_and_force_is_admin_only(authority):
    drain(authority, owner='agent-1')
    error = authority.refused(ROLL, 'claims/w1/drain', {'ttl_seconds': 60, 'owner': 'rollout'})
    assert error.status == 409 and str(error).endswith('drain_held_by:agent-1')
    assert authority.refused(ROLL, 'claims/w1/enable', {'owner': 'rollout'}).status == 409
    assert authority.refused(ROLL, 'claims/w1/drain', {'ttl_seconds': 60, 'force': True}).status == 403
    forced = authority.call(ADMIN, 'claims/w1/drain', {'ttl_seconds': 60, 'owner': 'ops', 'force': True})
    assert forced['owner'] == 'ops'
    ledger = [json.loads(line) for line in (authority.config.parent/'rollout-actions.jsonl').read_text().splitlines()]
    assert [r['forced'] for r in ledger if r['kind'] == 'drain'] == [False, True]


def test_stale_generation_loses_the_race(authority):
    first = drain(authority, owner='agent-1')
    second = authority.call(ADMIN, 'claims/w1/enable', {'owner': 'agent-1', 'if_generation': first['generation']})
    error = authority.refused(ADMIN, 'claims/w1/drain',
                              {'owner': 'agent-2', 'ttl_seconds': 60, 'if_generation': first['generation']})
    assert error.status == 409 and 'claim_generation_conflict' in str(error)
    assert authority.call(ADMIN, 'claims/w1')['generation'] == second['generation']  # the winner's value stands


def test_two_writers_on_different_workers_do_not_erase_each_other(authority):
    """The whole-file read-modify-write failure: here each write touches one row."""
    drain(authority, worker='w1', owner='agent-1')
    drain(authority, worker='w2', owner='agent-2')
    claims = {c['worker']: c for c in authority.call(ADMIN, 'claims')['claims']}
    assert claims['w1']['draining'] and claims['w2']['draining']


def test_expired_drain_reenables_lazily_and_on_the_tick(authority):
    worker = authority.client(W1)
    worker.request('worker/report', REPORT)
    drain(authority, ttl_seconds=60, owner='agent-1')
    authority.now[0] += 61
    worker.request('worker/report', REPORT)
    # Lazy: no tick has run, the next claim already sees the worker enabled.
    authority.client(A).submit(dict(version=1, key='k', handler='test.v1',
                                    input_digest=authority.digest, need={'cpu': 1}))
    assert worker.request('worker/claim', {'boot': 'b1'})['assignment'] is not None
    assert authority.server.claims.expire() == ['w1']  # the tick records it
    row = authority.call(ADMIN, 'claims/w1')
    assert row['enabled'] and row['reason'] == 'drain_expired'
    kinds = [json.loads(l)['kind'] for l in (authority.config.parent/'rollout-actions.jsonl').read_text().splitlines()]
    assert 'drain_expired' in kinds


def test_needs_operator_does_not_expire_into_service(authority):
    claim = authority.call(ROLL, 'claims/w1/drain', {'ttl_seconds': 60, 'needs_operator': True, 'owner': 'rollout'})
    assert claim['needs_operator']
    authority.now[0] += 10_000
    assert authority.server.claims.expire() == []
    assert authority.call(ADMIN, 'claims')['claims'][0]['draining'] is True
    assert authority.refused(ROLL, 'claims/w1/enable', {'owner': 'rollout'}).status == 403
    assert authority.call(ADMIN, 'claims/w1/enable', {'owner': 'ops'})['enabled'] is True


def test_roster_names_owner_and_expiry(authority):
    authority.client(W1).request('worker/report', REPORT)
    drain(authority, owner='agent-1', reason='roll image handler', ttl_seconds=300)
    entry = next(w for w in authority.call(ADMIN, 'workers')['workers'] if w['id'] == 'w1')
    assert entry['claim_enabled'] is False and entry['drain']['owner'] == 'agent-1'
    assert entry['drain']['expires_in_s'] == 300 and not entry['eligible']


def test_unknown_worker_and_caller_cannot_write(authority):
    assert authority.refused(ADMIN, 'claims/nope/drain', {'ttl_seconds': 60}).status == 404
    assert authority.refused(A, 'claims/w1/drain', {'ttl_seconds': 60}).status == 403
    assert authority.call(A, 'claims')['claims'] is not None  # callers may read


def test_claim_lookup_is_one_statement_whatever_the_worker_count(tmp_path):
    def statements(count):
        store = WorkloadStore(tmp_path/f's{count}.db', handlers={'test.v1'})
        workers = [Principal(id=f'w{i}', token=f'{i:032d}', role='worker', worker=f'w{i}', host=f'h{i}')
                   for i in range(count)]
        claims_module.Claims(store).sync_file(workers)
        seen = []
        db = store.connect()
        db.set_trace_callback(seen.append)
        claims_module.draining(db, store.clock(), {p.id: p for p in workers})
        return [s for s in seen if 'worker_claims' in s]
    assert len(statements(4)) == len(statements(40)) == 1


# ---- authority.json migration ---------------------------------------------------------------------

def test_file_value_is_imported_once_and_api_drain_survives_sighup(authority, caplog):
    assert authority.call(ADMIN, 'claims/w1')['owner'] == claims_module.IMPORT_OWNER
    drain(authority, owner='agent-1')
    with caplog.at_level('INFO'):
        assert authority.reload()  # the file still says claim_enabled true
    assert authority.call(ADMIN, 'claims/w1')['owner'] == 'agent-1'  # file did not override the API drain
    assert any('claim_enabled_in_file_ignored:w1' in r.message for r in caplog.records)


def test_legacy_file_edit_and_sighup_still_drain_and_enable(authority, caplog):
    """Other agents' scripts edit authority.json + SIGHUP; that path keeps working and is logged."""
    plist = principals()
    plist[3] = dict(plist[3], claim_enabled=False)
    authority.write(plist)
    with caplog.at_level('WARNING'):
        assert authority.reload()
    row = authority.call(ADMIN, 'claims/w1')
    assert row['enabled'] is False and row['owner'] == claims_module.FILE_OWNER and row['expires_at'] is None
    assert any('claim_file_edit_applied:w1' in r.message for r in caplog.records)
    authority.client(W1).request('worker/report', REPORT)
    authority.client(A).submit(dict(version=1, key='k', handler='test.v1',
                                    input_digest=authority.digest, need={'cpu': 1}))
    assert authority.client(W1).request('worker/claim', {'boot': 'b1'})['reason'] == 'worker_draining'
    authority.write(principals())
    assert authority.reload()
    assert authority.call(ADMIN, 'claims/w1')['enabled'] is True
    assert authority.client(W1).request('worker/claim', {'boot': 'b1'})['assignment']


def test_torn_file_keeps_claims_and_is_reported(authority):
    drain(authority, owner='agent-1')
    authority.config.write_text('{"principals": [')
    assert authority.reload() is False
    assert authority.call(ADMIN, 'claims/w1')['owner'] == 'agent-1'
    status = authority.call(ADMIN, 'reload/status')
    assert status['last_attempt']['outcome'] == 'refused' and status['verdict'] == 'refused_current_file'


# ---- reload/status ---------------------------------------------------------------------------------

def test_reload_status_tells_edited_not_signalled_from_applied(authority):
    authority.server.reload_status.applied_now(authority.config, file_hash(authority.config), 5, 'startup')
    assert authority.call(ADMIN, 'reload/status')['verdict'] == 'applied'
    authority.write(principals(extra=[dict(id='new', token='n'*32, role='worker', worker='new', host='h9')]))
    status = authority.call(ADMIN, 'reload/status')
    assert status['verdict'] == 'edited_not_applied' and status['in_sync'] is False
    assert authority.reload()
    assert authority.call(ADMIN, 'reload/status')['verdict'] == 'applied'
    assert authority.refused(A, 'reload/status').status == 403


def test_old_database_without_claims_rows_falls_back_to_the_principal(tmp_path):
    """A store opened without the authority's sync (tests, tools) still honours claim_enabled."""
    store = WorkloadStore(tmp_path/'x.db', handlers={'test.v1'})
    drained = Principal(id='w9', token='9'*32, role='worker', worker='w9', host='h', claim_enabled=False)
    with store.transaction() as db:
        assert claims_module.draining(db, store.clock(), {drained.id: drained}) == {'w9'}
    assert sqlite3.connect(store.path).execute('SELECT COUNT(*) FROM worker_claims').fetchone()[0] == 0
