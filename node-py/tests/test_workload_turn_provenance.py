"""Real authenticated HTTP and SQLite source joins, not handcrafted job rows."""
from dataclasses import replace
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor
import json

import pytest

from livestack_node.workloads.http import Principal
from livestack_node.workloads.model import Limits
from livestack_node.workloads.store import WorkloadStore
from test_workload_job_changes import Authority, SOURCE_SUBMIT, READER, ALICE, W, REPORT


@pytest.fixture
def authority(tmp_path):
    made = Authority(tmp_path)
    made.server.replace_principals([
        replace(p, provenance_kinds=('overlord_turn',)) if p.id == 'source-submitter' else p
        for p in made.server.principals
    ])
    yield made
    made.server.shutdown()
    made.server.server_close()


def submit(a, key='source', turn='turn_a', token=SOURCE_SUBMIT, **extra):
    return a.call('jobs', dict(version=1, key=key, handler='test.v1', input_digest=a.digest,
        need={'cpu': 1}, labels={'owner': 'source-owner'},
        provenance={'v': 1, 'kind': 'overlord_turn', 'turn_id': turn}, **extra), token=token)


def read(a, turn='turn_a', suffix=''):
    return a.call('jobs/changes?source_turn_id=' + turn + suffix, token=READER)


def test_actual_source_stamps_and_worker_attempt_come_from_authority(authority):
    status, job = submit(authority)
    assert status == 200
    status, page = read(authority)
    assert status == 200 and page['owner'] == 'source-owner'
    row, = page['jobs']
    assert row['id'] == job['id'] and row['source_kind'] == 'overlord_turn'
    assert row['source_turn_id'] == 'turn_a' and row['source_principal'] == 'source-submitter'
    assert row['current_explanation'] == {'code': 'queued', 'observed_ms': row['updated_ms']}
    assert 'attempt' not in row
    authority.call('worker/report', REPORT, token=W)
    authority.now[0] += 1
    assignment = authority.call('worker/claim', {'boot': 'b1'}, token=W)[1]['assignment']
    row, = read(authority)[1]['jobs']
    assert row['state'] == 'running' and row['attempt']['worker'] == 'w1'
    assert row['attempt']['id'] == assignment['attempt_id']
    assert row['current_explanation']['code'] == 'running'
    with authority.store.transaction() as db:
        raw = db.execute('SELECT source_provenance,spec,labels FROM jobs WHERE id=?', (job['id'],)).fetchone()
        stamp = json.loads(raw['source_provenance'])
        assert stamp['principal'] == 'source-submitter' and stamp['owner'] == 'source-owner'
        assert 'turn_a' not in raw['spec'] and 'turn_a' not in raw['labels']


def test_fixed_owner_and_exact_turn_do_not_expose_sibling_jobs(authority):
    own = submit(authority)[1]
    submit(authority, key='sibling-turn', turn='turn_b')
    request = dict(version=1, key='sibling-owner', handler='test.v1', input_digest=authority.digest,
        need={'cpu': 1}, labels={'owner': 'source-owner-other'},
        provenance={'v': 1, 'kind': 'overlord_turn', 'turn_id': 'turn_a'})
    assert authority.call('jobs', request, token=SOURCE_SUBMIT)[0] == 200
    assert [r['id'] for r in read(authority)[1]['jobs']] == [own['id']]
    assert read(authority, 'absent_turn')[1]['jobs'] == []
    assert authority.call('jobs/changes?source_turn_id=turn_a', token=ALICE)[0] == 403


@pytest.mark.parametrize('source', [None, {}, {'v': True, 'kind': 'overlord_turn', 'turn_id': 'turn_a'},
    {'v': 1, 'kind': 'overlord_turn', 'turn_id': ''},
    {'v': 1, 'kind': 'overlord_turn', 'turn_id': 'a' * 129},
    {'v': 1, 'kind': 'overlord_turn', 'turn_id': 'turn_a', 'principal': 'forged'},
    {'v': 1, 'kind': 'overlord_turn', 'turn_id': 'turn_a', 'worker': 'forged'}])
def test_invalid_or_forged_envelopes_never_create_a_job(authority, source):
    request = dict(version=1, key='bad', handler='test.v1', input_digest=authority.digest,
        need={'cpu': 1}, labels={'owner': 'source-owner'}, provenance=source)
    assert authority.call('jobs', request, token=SOURCE_SUBMIT)[0] == 400
    assert read(authority)[1]['jobs'] == []


def test_ungranted_caller_and_foreign_delegation_are_refused(authority):
    request = dict(version=1, key='bad', handler='test.v1', input_digest=authority.digest,
        need={'cpu': 1}, provenance={'v': 1, 'kind': 'overlord_turn', 'turn_id': 'turn_a'})
    assert authority.call('jobs', request, token=ALICE)[0] == 403
    request['labels'] = {'owner': 'alice'}
    assert authority.call('jobs', request, token=SOURCE_SUBMIT)[0] == 403
    assert read(authority)[1]['jobs'] == []


def test_advisory_labels_cannot_impersonate_source(authority):
    request = dict(version=1, key='bad', handler='test.v1', input_digest=authority.digest,
        need={'cpu': 1}, labels={'owner': 'source-owner', 'source_turn_id': 'turn_a'})
    status, failure = authority.call('jobs', request, token=SOURCE_SUBMIT)
    assert status == 400 and failure['error'] == 'provenance_labels_forbidden'


def test_complete_authority_stamp_is_byte_bounded_before_a_job_exists(authority):
    producer, owner, kind, token = 'p' * 128, 'o' * 128, 'k' * 128, 'z' * 32
    authority.server.replace_principals([*authority.server.principals,
        Principal(producer, token, 'caller', ('test.v1',), delegate_prefix=owner,
                  provenance_kinds=(kind,))])
    authority.server.blobs.put(producer, authority.digest, 5, BytesIO(b'input'))
    request = dict(version=1, key='too-large', handler='test.v1', input_digest=authority.digest,
        need={'cpu': 1}, labels={'owner': owner},
        provenance={'v': 1, 'kind': kind, 'turn_id': 't' * 128})
    assert authority.call('jobs', request, token=token)[0] == 413
    with authority.store.transaction() as db:
        assert db.execute('SELECT count(*) FROM jobs').fetchone()[0] == 0


@pytest.mark.parametrize('query', ['source_turn_id=', 'source_turn_id=a%20b',
    'source_turn_id=a&source_turn_id=b', 'source_turn_id=' + 't' * 129,
    'source_turn_id=a&owner=alice'])
def test_source_query_cannot_widen_owner_or_weaken_identity(authority, query):
    assert authority.call('jobs/changes?' + query, token=READER)[0] == 400


def test_producer_reads_only_its_exact_turn_and_explicit_authorized_owner(authority):
    own = submit(authority)[1]
    submit(authority, key='other-turn', turn='turn_b')
    path = 'jobs/changes?source_turn_id=turn_a&owner=source-owner'
    status, page = authority.call(path, token=SOURCE_SUBMIT)
    assert status == 200 and page['owner'] == 'source-owner'
    assert [row['id'] for row in page['jobs']] == [own['id']]
    assert authority.call('jobs/changes?source_turn_id=turn_a', token=SOURCE_SUBMIT)[1]['jobs'] == []
    assert authority.call('jobs/changes', token=SOURCE_SUBMIT)[0] == 403
    assert authority.call('jobs/changes?owner=source-owner', token=SOURCE_SUBMIT)[0] == 403
    assert authority.call('jobs/changes?source_turn_id=turn_a&owner=alice', token=SOURCE_SUBMIT)[0] == 403


def test_producer_cannot_read_a_sibling_producers_same_owner_turn(authority):
    token = 'n' * 32
    authority.server.replace_principals([*authority.server.principals,
        Principal('second-producer', token, 'caller', ('test.v1',), delegate_prefix='source-owner',
                  provenance_kinds=('overlord_turn',))])
    authority.server.blobs.put('second-producer', authority.digest, 5, BytesIO(b'input'))
    first = submit(authority)[1]
    assert submit(authority, key='other-producer', token=token)[0] == 200
    path = 'jobs/changes?source_turn_id=turn_a&owner=source-owner'
    rows = authority.call(path, token=SOURCE_SUBMIT)[1]['jobs']
    assert [row['id'] for row in rows] == [first['id']]
    assert len(read(authority)[1]['jobs']) == 2  # fixed owner reader retains its declared scope


def test_replay_preserves_source_and_conflicting_turn_cannot_relabel(authority):
    first = submit(authority)[1]
    assert submit(authority)[1]['id'] == first['id']
    assert submit(authority, turn='turn_b')[0] == 409
    assert read(authority, 'turn_b')[1]['jobs'] == []
    assert read(authority)[1]['jobs'][0]['id'] == first['id']


def test_concurrent_turns_cannot_relabel_one_idempotent_job(authority):
    with ThreadPoolExecutor(max_workers=2) as pool:
        replies = list(pool.map(lambda turn: submit(authority, turn=turn), ['turn_a', 'turn_b']))
    assert sorted(status for status, _ in replies) == [200, 409]
    assert sum(len(read(authority, turn)[1]['jobs']) for turn in ['turn_a', 'turn_b']) == 1


def test_additive_migration_preserves_a_legacy_job_and_its_replay(tmp_path):
    path = tmp_path / 'legacy.db'
    store = WorkloadStore(path, handlers={'test.v1'})
    request = dict(version=1, key='legacy-key', handler='test.v1', input_digest='a' * 64, need={'cpu': 1})
    original = store.submit('legacy', request)
    with store.transaction() as db:
        db.execute('DROP INDEX jobs_source_turn_changes')
        db.execute('DROP INDEX jobs_source_expiry')
        db.execute('ALTER TABLE jobs DROP COLUMN source_provenance')
        db.execute('ALTER TABLE jobs DROP COLUMN source_observed')
    reopened = WorkloadStore(path, handlers={'test.v1'})
    assert reopened.submit('legacy', request)['id'] == original['id']
    with reopened.transaction() as db:
        row = db.execute('SELECT source_provenance,source_observed FROM jobs WHERE id=?', (original['id'],)).fetchone()
        assert tuple(row) == (None, None)


def test_retained_job_survives_source_expiry_without_hidden_duplicate_metadata(authority):
    authority.store.limits = replace(authority.store.limits, provenance_seconds=60)
    job = submit(authority, retain=True)[1]
    authority.now[0] += 60
    assert read(authority)[1]['jobs'] == []  # read path enforces boundary, without waiting for sweep
    authority.store.sweep()
    with authority.store.transaction() as db:
        row = db.execute('SELECT * FROM jobs WHERE id=?', (job['id'],)).fetchone()
        assert row is not None and row['retain'] == 1
        assert row['source_provenance'] is None and row['source_observed'] is None
        assert 'turn_a' not in row['spec'] and 'turn_a' not in row['labels']


def test_normal_sweep_clears_source_without_a_source_read(authority):
    authority.store.limits = replace(authority.store.limits, provenance_seconds=60)
    job = submit(authority, retain=True)[1]
    authority.now[0] += 61
    authority.store.sweep()
    with authority.store.transaction() as db:
        row = db.execute('SELECT retain,source_provenance,source_observed FROM jobs WHERE id=?',
                         (job['id'],)).fetchone()
        assert tuple(row) == (1, None, None)


def test_exact_turn_cursor_handles_ties_and_uses_composite_index(authority):
    expected = {submit(authority, key='same-time-' + str(i))[1]['id'] for i in range(5)}
    seen = set(); suffix = '&limit=2'
    for _ in range(4):
        status, page = read(authority, suffix=suffix)
        assert status == 200 and len(page['jobs']) <= 2
        seen.update(row['id'] for row in page['jobs'])
        if not page['truncated']:
            break
        cursor = page['next_cursor']
        suffix = '&limit=2&updated_since=' + str(cursor['updated_since']) + '&after_job_id=' + cursor['after_job_id']
    assert seen == expected
    with authority.store.transaction() as db:
        plan = db.execute("EXPLAIN QUERY PLAN SELECT id FROM jobs WHERE "
            "COALESCE(NULLIF(json_extract(labels,'$.owner'),''),owner)=? AND "
            "json_extract(source_provenance,'$.turn_id')=? ORDER BY CAST(updated*1000 AS INTEGER),id LIMIT 33",
            ('source-owner', 'turn_a')).fetchall()
        assert any('jobs_source_turn_changes' in str(tuple(row)) for row in plan)


def test_source_metadata_cannot_expand_a_page_past_its_wire_byte_bound(authority):
    for i in range(100):
        assert submit(authority, key='bounded-' + str(i))[0] == 200
    status, page = read(authority, suffix='&limit=256')
    assert status == 200 and 0 < len(page['jobs']) < 100 and page['truncated'] == 1
    assert len(json.dumps(page, separators=(',', ':'), sort_keys=True).encode()) <= 30 * 1024
    last = page['jobs'][-1]
    assert page['next_cursor'] == {'updated_since': last['updated_ms'], 'after_job_id': last['id']}


@pytest.mark.parametrize('seconds', [None, 0, True, float('inf'), 604801])
def test_retention_cannot_be_unbounded(seconds):
    with pytest.raises(ValueError, match='provenance_seconds'):
        Limits(provenance_seconds=seconds)


@pytest.mark.parametrize('role', ['worker', 'job_reader'])
def test_nonproducer_roles_cannot_receive_source_grants(role):
    with pytest.raises(ValueError, match='provenance_kinds'):
        Principal('forbidden', 'x' * 32, role, provenance_kinds=('overlord_turn',))
