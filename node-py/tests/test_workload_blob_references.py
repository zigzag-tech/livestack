"""Real SQLite/CAS and authenticated HTTP retention roots; no fake blob store."""
import hashlib
from io import BytesIO
import json
from threading import Thread
import urllib.request
import urllib.error

import pytest

from livestack_node.workloads.blobs import BlobStore
from livestack_node.workloads.blob_references import BlobReferences, MAX_REFERENCES
from livestack_node.workloads.http import WorkloadServer, Principal
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore


def put(blobs, value=b'captured source', owner='alice'):
    digest = hashlib.sha256(value).hexdigest()
    blobs.put(owner, digest, len(value), BytesIO(value))
    return digest


def test_named_roots_survive_restart_and_prune_then_release_independently(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: now[0])
    blobs = BlobStore(store, tmp_path/'objects', retention_seconds=10)
    digest = put(blobs)
    other = put(blobs, b'not retained')
    refs = BlobReferences(blobs)
    first = refs.replace('alice', 'published', [digest, digest], 0)
    assert first == dict(revision=1, digests=[digest])
    assert refs.replace('alice', 'published', [digest], 0) == first
    refs.replace('alice', 'release-input', [digest], 0)
    now[0] += 20
    blobs = BlobStore(WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: now[0]),
                      tmp_path/'objects', retention_seconds=10)
    refs = BlobReferences(blobs)
    assert refs.get('alice', 'published') == first
    blobs.prune()
    assert (tmp_path/'objects'/digest).read_bytes() == b'captured source'
    assert not (tmp_path/'objects'/other).exists()
    assert refs.replace('alice', 'published', [], 1)['revision'] == 2
    with pytest.raises(WorkloadError, match='revision conflict'):
        refs.replace('alice', 'published', [digest], 1)
    blobs.prune()
    assert (tmp_path/'objects'/digest).exists(), 'another named root independently retains it'
    refs.replace('alice', 'release-input', [], 1)
    blobs.prune()
    assert not (tmp_path/'objects'/digest).exists()
    assert refs.get('alice', 'published') == dict(revision=2, digests=[]), 'empty root keeps its fence'


def test_reference_ownership_conflicts_and_structural_bounds(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    blobs = BlobStore(store, tmp_path/'objects')
    digest = put(blobs)
    refs = BlobReferences(blobs)
    assert refs.get('bob', 'published') == dict(revision=0, digests=[])
    with pytest.raises(WorkloadError, match='not found'):
        refs.replace('bob', 'published', [digest], 0)
    assert refs.get('bob', 'published')['revision'] == 0
    for bad in [True, -1, 1.5, '1', 9007199254740991]:
        with pytest.raises(WorkloadError, match='revision'):
            refs.replace('alice', 'published', [], bad)
    with pytest.raises(WorkloadError, match='digest limit'):
        refs.replace('alice', 'published', [digest]*17, 0)
    refs.replace('alice', 'published', [digest], 0)
    with store.transaction() as db:
        db.executemany('INSERT INTO blob_references VALUES(?,?,1,?)',
                       [('alice',f'root-{i}','[]') for i in range(MAX_REFERENCES-1)])
    with pytest.raises(WorkloadError, match='capacity'):
        refs.replace('alice', 'one-too-many', [], 0)
    assert refs.replace('alice', 'published', [], 1)['revision'] == 2, 'release still works at capacity'


def test_http_references_are_caller_scoped_and_protect_real_bytes(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: now[0])
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('alice', 'a'*32, 'caller', ('test.v1',)),
        Principal('bob', 'b'*32, 'caller', ('test.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='w1', host='host1')])
    server.blobs.retention_seconds = 10
    digest = put(server.blobs)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    def call(body=None, token='a'*32):
        req = urllib.request.Request(f'http://127.0.0.1:{server.server_port}/v1/workloads/references/integration',
            data=json.dumps(body).encode() if body is not None else None,
            headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            return error.code, json.load(error)
    try:
        assert call(token='bad')[0] == 401
        assert call(token='w'*32)[0] == 403
        assert call(dict(digests=[digest], expected_revision=0), token='b'*32)[0] == 404
        assert call(dict(digests=[digest], expected_revision=0, owner='alice'))[0] == 400
        assert call(dict(digests=[digest], expected_revision=0)) == (200, dict(revision=1, digests=[digest]))
        assert call(token='b'*32) == (200, dict(revision=0, digests=[]))
        now[0] += 20
        server.blobs.prune()
        assert (server.blobs.root/digest).exists()
        assert call(dict(digests=[], expected_revision=0))[0] == 409
        assert call(dict(digests=[], expected_revision=1))[0] == 200
        server.blobs.prune()
        assert not (server.blobs.root/digest).exists()
    finally:
        server.shutdown(); thread.join(timeout=5); server.server_close()


def test_competing_replacements_have_one_winner_and_failed_updates_are_atomic(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    blobs = BlobStore(store, tmp_path/'objects')
    refs = BlobReferences(blobs)
    original, left, right = [put(blobs, value) for value in [b'original', b'left', b'right']]
    refs.replace('alice', 'published', [original], 0)
    def replace(digest):
        try:
            return refs.replace('alice', 'published', [digest], 1)
        except WorkloadError as error:
            assert error.status == 409
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(replace, [left, right]))
    assert len([result for result in results if result is not None]) == 1
    current = refs.get('alice', 'published')
    assert current['revision'] == 2
    with pytest.raises(WorkloadError, match='not found'):
        refs.replace('alice', 'published', [original, '0'*64], 2)
    assert refs.get('alice', 'published') == current
