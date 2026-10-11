"""Bounded admin and fixed-owner incremental job views against HTTP and SQLite."""
import hashlib
import json
import urllib.error
import urllib.request
from io import BytesIO
from threading import Thread

import pytest

from livestack_node.workloads import job_changes
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore

ALICE, BOB, ADMIN, W = 'a'*32, 'b'*32, 'm'*32, 'w'*32
SOURCE_SUBMIT, READER = 's'*32, 'r'*32
REPORT = dict(boot='b1', report=dict(capacity={'cpu': 8}, available={'cpu': 8}, labels={},
                                     handlers=['test.v1'], ready=True))


class Authority:
    def __init__(self, tmp_path):
        self.now = [1_000_000.0]
        self.store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: self.now[0])
        self.server = WorkloadServer(('127.0.0.1', 0), self.store, [
            Principal('alice', ALICE, 'caller', ('test.v1',)),
            Principal('bob', BOB, 'caller', ('test.v1',)),
            Principal('ops', ADMIN, 'admin', ('test.v1',)),
            Principal('source-submitter', SOURCE_SUBMIT, 'caller', ('test.v1',),
                     delegate_prefix='source-owner'),
            Principal('source-owner', READER, 'job_reader'),
            Principal('worker', W, 'worker', worker='w1', host='host1'),
        ])
        data = b'input'
        self.digest = hashlib.sha256(data).hexdigest()
        for owner in ('alice', 'bob', 'source-submitter'):
            self.server.blobs.put(owner, self.digest, len(data), BytesIO(data))
        Thread(target=self.server.serve_forever, daemon=True).start()

    def call(self, path, data=None, token=ADMIN):
        req = urllib.request.Request(f'http://127.0.0.1:{self.server.server_port}/v1/workloads/{path}',
            data=json.dumps(data).encode() if data is not None else None,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)

    def submit(self, token, key):
        return self.call('jobs', dict(version=1, key=key, handler='test.v1', input_digest=self.digest,
                                      need={'cpu': 1}), token=token)[1]

    def submit_source(self, key):
        return self.call('jobs', dict(version=1, key=key, handler='test.v1', input_digest=self.digest,
                                      need={'cpu': 1}, labels={'owner': 'source-owner'}),
                         token=SOURCE_SUBMIT)[1]


@pytest.fixture
def authority(tmp_path):
    made = Authority(tmp_path)
    yield made
    made.server.shutdown()
    made.server.server_close()


def test_a_job_is_followed_from_queued_to_failed_with_the_authoritys_own_outcome(authority):
    job = authority.submit(ALICE, 'k1')
    status, page = authority.call('job-changes')
    assert status == 200 and page['truncated'] == 0
    row = page['jobs'][0]
    assert (row['id'], row['owner'], row['handler'], row['state']) == (job['id'], 'alice', 'test.v1', 'queued')
    assert row['requires'] == 'cpu=1' and 'attempt' not in row and 'outcome' not in row
    assert row['queued_ms'] == 1_000_000_000

    authority.call('worker/report', REPORT, token=W)
    authority.now[0] += 5
    assignment = authority.call('worker/claim', {'boot': 'b1'}, token=W)[1]['assignment']
    _, running = authority.call(f'job-changes?updated_since={row["updated_ms"]}')
    assert [(j['state'], j['attempt']['worker']) for j in running['jobs']] == [('running', 'w1')]
    assert running['jobs'][0]['attempt']['started_ms'] == 1_000_005_000

    authority.now[0] += 5
    authority.call('worker/complete', dict(boot='b1', attempt_id=assignment['attempt_id'], fence=assignment['fence'],
                                           input_digest=authority.digest, outcome='product_failure',
                                           result={'error': 'tests failed', 'exit_code': 1}), token=W)
    _, done = authority.call(f'job-changes?updated_since={running["jobs"][0]["updated_ms"]}')
    final = done['jobs'][0]
    assert final['state'] == 'failed' and final['finished_ms'] == 1_000_010_000
    assert final['outcome'] == {'kind': 'product', 'reason': 'tests failed'}
    assert authority.call(f'job-changes?updated_since={final["updated_ms"]}')[1]['jobs'] == []


def test_the_owner_filter_isolates_accounts_and_only_admins_may_read(authority):
    authority.submit(ALICE, 'a1')
    authority.submit(BOB, 'b1')
    _, bobs = authority.call('job-changes?owner=bob')
    assert [j['owner'] for j in bobs['jobs']] == ['bob'] and bobs['truncated'] == 0
    assert len(authority.call('job-changes')[1]['jobs']) == 2
    assert authority.call('job-changes', token=ALICE)[0] == 403
    assert authority.call('job-changes', token='x'*32)[0] == 401


def test_bad_queries_are_refused_by_name(authority):
    for query in ('updated_since=-1', 'updated_since=abc', 'limit=0', 'limit=x', 'bogus=1', 'owner=a&owner=b'):
        status, body = authority.call('job-changes?' + query)
        assert status == 400, (query, body)


def test_the_page_is_capped_and_resuming_skips_nothing(authority):
    for i in range(7):
        authority.submit(ALICE, f'k{i}')
        authority.now[0] += 0.001 * (i % 2)   # some jobs share a millisecond
    seen, since = [], 0
    for _ in range(10):
        _, page = authority.call(f'job-changes?updated_since={since}&limit=3')
        assert len(page['jobs']) <= 3
        if not page['jobs']:
            break
        seen += [j['id'] for j in page['jobs']]
        since = page['jobs'][-1]['updated_ms']
    assert len(seen) == len(set(seen)) == 7
    _, first = authority.call('job-changes?limit=3')
    assert first['truncated'] == 7 - len(first['jobs']) > 0
    _, huge = authority.call('job-changes?limit=100000')
    assert len(huge['jobs']) == 7   # limit is clamped to the 256 cap, not refused


def test_the_response_stays_under_the_byte_bound(authority, monkeypatch):
    for i in range(30):
        authority.submit(ALICE, f'k{i}')
        authority.now[0] += 1
    monkeypatch.setattr(job_changes, 'MAX_BYTES', 4096)
    _, page = authority.call('job-changes')
    assert 0 < len(page['jobs']) < 30
    assert len(json.dumps(page, separators=(',', ':')).encode()) <= 4096
    assert page['truncated'] == 30 - len(page['jobs'])


def test_job_reader_principals_are_fixed_owner_and_have_no_other_privileges():
    reader = Principal('source-owner', READER, 'job_reader')
    assert reader.handlers == () and reader.delegate_prefix is None and reader.worker is None
    invalid = (
        {'handlers': ('test.v1',)},
        {'delegate_prefix': 'source-'},
        {'worker': 'w1', 'host': 'host1'},
        {'max_running': 1},
        {'on_cap': 'refuse'},
        {'claim_enabled': False},
        {'remote_job': 'a'*32, 'remote_provider': 'github', 'remote_run_id': '1', 'remote_boot': 'b1'},
        {'upload_grants': True},
        {'upload_base_url': 'http://127.0.0.1'},
        {'benchday_host_id': 'host-id'},
    )
    for options in invalid:
        with pytest.raises(ValueError, match='job_reader'):
            Principal('source-owner', READER, 'job_reader', **options)


def test_the_reader_route_derives_and_reports_its_fixed_effective_owner(authority):
    source = authority.submit_source('source-1')
    authority.submit(BOB, 'bob-1')
    status, page = authority.call('jobs/changes', token=READER)
    assert status == 200
    assert page['owner'] == 'source-owner' and page['truncated'] == 0
    assert [row['id'] for row in page['jobs']] == [source['id']]
    assert page['jobs'][0]['owner'] == 'source-owner'
    assert page['next_cursor'] == {
        'updated_since': page['jobs'][0]['updated_ms'], 'after_job_id': source['id']}
    assert authority.call('jobs/changes', token=ALICE)[0] == 403
    assert authority.call('jobs/changes', token=ADMIN)[0] == 403
    assert authority.call('jobs/changes', token='x'*32)[0] == 401
    # The old owner-selecting endpoint remains a compatible admin-only view.
    assert authority.call('job-changes?owner=bob', token=ADMIN)[0] == 200
    assert authority.call('job-changes?owner=bob', token=READER)[0] == 403


def test_the_reader_token_is_refused_on_all_other_routes(authority):
    source = authority.submit_source('source-1')
    paths = (
        ('jobs', {}),
        (f'jobs/{source["id"]}/cancel', {}),
        ('jobs/changes', {}),
        ('status', None),
        ('workers', None),
        (f'objects/{authority.digest}', None),
        ('job-changes', None),
    )
    for path, body in paths:
        assert authority.call(path, body, token=READER)[0] == 403, path


def test_reader_queries_are_strict_and_timestamp_ties_resume_without_gaps(authority):
    made = [authority.submit_source(f'tie-{i}') for i in range(3)]
    bad = (
        'owner=source-owner', 'updated_since=-1', 'updated_since=abc', 'updated_since=1&owner=x',
        'limit=0', 'limit=x', 'bogus=1', 'limit=1&limit=2', 'updated_since=1&updated_since=2',
        'after_job_id=x', 'updated_since=9223372036854775808',
        'updated_since=0&after_job_id=a&limit=1&extra=x',
    )
    for query in bad:
        status, body = authority.call('jobs/changes?' + query, token=READER)
        assert status == 400, (query, body)

    first_status, first = authority.call('jobs/changes?updated_since=0&limit=2', token=READER)
    assert first_status == 200 and len(first['jobs']) == 2 and first['truncated'] > 0
    cursor = first['next_cursor']
    second_status, second = authority.call(
        f"jobs/changes?updated_since={cursor['updated_since']}&after_job_id={cursor['after_job_id']}&limit=2",
        token=READER)
    assert second_status == 200 and len(second['jobs']) == 1 and second['truncated'] == 0
    ids = [row['id'] for row in first['jobs'] + second['jobs']]
    assert ids == sorted(job['id'] for job in made) and len(ids) == len(set(ids))
    # A new timestamp-only poll replays the watermark boundary for deduplication.
    _, replay = authority.call(f"jobs/changes?updated_since={first['jobs'][0]['updated_ms']}", token=READER)
    assert [row['id'] for row in replay['jobs']] == ids


def test_reader_pages_cap_rows_and_wire_bytes(authority, monkeypatch):
    for i in range(260):
        authority.submit_source(f'cap-{i}')
    # Raise only the test module's byte ceiling to prove the independent row cap.
    monkeypatch.setattr(job_changes, 'MAX_BYTES', 1024 * 1024)
    _, first = authority.call('jobs/changes?limit=256', token=READER)
    assert len(first['jobs']) == 256 and first['truncated'] > 0
    cursor = first['next_cursor']
    _, second = authority.call(
        f"jobs/changes?updated_since={cursor['updated_since']}&after_job_id={cursor['after_job_id']}",
        token=READER)
    assert len(second['jobs']) == 4 and second['truncated'] == 0

    monkeypatch.setattr(job_changes, 'MAX_BYTES', 4096)
    _, small = authority.call('jobs/changes', token=READER)
    assert 0 < len(small['jobs']) < 256 and small['truncated'] > 0
    assert len(json.dumps(small, sort_keys=True, separators=(',', ':')).encode()) <= 4096
    assert small['next_cursor']['after_job_id'] == small['jobs'][-1]['id']


def test_workload_client_checks_owner_rows_cursor_and_response_size(authority):
    authority.submit_source('client-1')
    client = WorkloadClient(
        f'http://127.0.0.1:{authority.server.server_port}', READER)
    try:
        page = client.list_job_changes(expected_owner='source-owner')
        assert page['owner'] == 'source-owner' and len(page['jobs']) == 1
    finally:
        client.close()

    valid = {
        'owner': 'source-owner', 'now_ms': 1, 'jobs': [
            {'id': 'job-1', 'owner': 'source-owner', 'updated_ms': 1}],
        'truncated': 0, 'next_cursor': {'updated_since': 1, 'after_job_id': 'job-1'},
    }

    def reader(response):
        client = object.__new__(WorkloadClient)
        client.request = lambda _route: response
        return client

    assert reader(valid).list_job_changes(expected_owner='source-owner') == valid
    for response in (
        {**valid, 'owner': 'other-owner'},
        {**valid, 'jobs': [{**valid['jobs'][0], 'owner': 'other-owner'}]},
        {**valid, 'next_cursor': {'updated_since': 2, 'after_job_id': 'job-1'}},
        {**valid, 'padding': 'x'*(30*1024)},
    ):
        with pytest.raises(WorkloadError):
            reader(response).list_job_changes(expected_owner='source-owner')
