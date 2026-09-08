"""Completed artifacts remain readable through real durable job references."""
import hashlib
from io import BytesIO

import pytest

from livestack_node.workloads.blobs import BlobStore
from livestack_node.workloads.model import Limits, WorkloadError
from livestack_node.workloads.store import WorkloadStore


@pytest.mark.parametrize('retain', [False, True])
@pytest.mark.parametrize('first_outcome', ['succeeded', 'infrastructure'])
def test_result_objects_survive_until_job_retention_releases_them(tmp_path, retain, first_outcome):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'build.v1'}, clock=lambda: now[0],
                          limits=Limits(terminal_seconds=100))
    blobs = BlobStore(store, tmp_path/'objects', retention_seconds=10)
    refs = []
    for data in (b'input', b'artifact'):
        digest = hashlib.sha256(data).hexdigest()
        blobs.put('alice', digest, len(data), BytesIO(data))
        refs.append(dict(digest=digest, size=len(data), name='binary'))
    job = store.submit('alice', dict(version=1,key='build',handler='build.v1',
        input_digest=refs[0]['digest'],need={'cpu':1},retain=retain))
    store.register('w','host','boot',dict(capacity={'cpu':2},available={'cpu':2},
        handlers=['build.v1'],labels={},ready=True))
    a = store.claim('w','boot')
    store.complete('w','boot',a['attempt_id'],a['fence'],input_digest=refs[0]['digest'],
                   outcome=first_outcome,result={'artifacts':[refs[1]]})
    if first_outcome == 'infrastructure':
        retry = store.claim('w','boot')
        store.complete('w','boot',retry['attempt_id'],retry['fence'],input_digest=refs[0]['digest'],
                       outcome='succeeded',result={})
    now[0] += 20
    blobs.prune()
    with blobs.open('alice', refs[1]['digest']) as (stream, _):
        assert stream.read() == b'artifact'
    now[0] += 200
    store.sweep()
    blobs.prune()
    if retain:
        with blobs.open('alice', refs[1]['digest']) as (stream, _):
            assert stream.read() == b'artifact'
    else:
        with pytest.raises(WorkloadError, match='not found'):
            with blobs.open('alice', refs[1]['digest']):
                pass


@pytest.mark.parametrize('kind', ['missing', 'other-owner', 'wrong-size', 'duplicate-name'])
def test_completion_refuses_invalid_or_foreign_artifact_references(tmp_path, kind):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'build.v1'})
    blobs = BlobStore(store, tmp_path/'objects')
    digest = hashlib.sha256(b'binary').hexdigest()
    if kind != 'missing':
        blobs.put('bob' if kind == 'other-owner' else 'alice', digest, 6, BytesIO(b'binary'))
    store.submit('alice',dict(version=1,key='build',handler='build.v1',input_digest='0'*64,need={'cpu':1}))
    store.register('w','host','boot',dict(capacity={'cpu':2},available={'cpu':2},
        handlers=['build.v1'],labels={},ready=True))
    a = store.claim('w','boot')
    ref = dict(name='binary',digest=digest,size=7 if kind == 'wrong-size' else 6)
    with pytest.raises(WorkloadError):
        store.complete('w','boot',a['attempt_id'],a['fence'],input_digest='0'*64,
            outcome='succeeded',result={'artifacts':[ref,ref] if kind == 'duplicate-name' else [ref]})
    assert store.get('alice',a['job_id'])['state'] == 'running'
