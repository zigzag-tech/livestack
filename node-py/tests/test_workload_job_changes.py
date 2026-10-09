"""`GET /v1/workloads/job-changes`: incremental, owner-filtered, bounded job listing
(pipelines-are-streams 4.1), against a real authority over HTTP and SQLite."""
import hashlib
import json
import urllib.error
import urllib.request
from io import BytesIO
from threading import Thread

import pytest

from livestack_node.workloads import job_changes
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore

ALICE, BOB, ADMIN, W = 'a'*32, 'b'*32, 'm'*32, 'w'*32
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
            Principal('worker', W, 'worker', worker='w1', host='host1'),
        ])
        data = b'input'
        self.digest = hashlib.sha256(data).hexdigest()
        for owner in ('alice', 'bob'):
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
