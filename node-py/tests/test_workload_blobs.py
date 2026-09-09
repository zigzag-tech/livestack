"""Real streamed objects, ownership boundaries, quota and crash cleanup."""
import hashlib
from io import BytesIO

import pytest

from livestack_node.workloads.blobs import BlobStore
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore


def test_stream_digest_owner_and_quota(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    blobs = BlobStore(store, tmp_path/'objects', max_bytes=8, max_object_bytes=8)
    data = b'12345678'
    digest = hashlib.sha256(data).hexdigest()
    blobs.put('alice', digest, 8, BytesIO(data))
    with blobs.open('alice', digest) as (stream,size):
        assert stream.read() == data and size == 8
    with pytest.raises(WorkloadError, match='not found'):
        with blobs.open('bob', digest):
            pass
    with pytest.raises(WorkloadError, match='mismatch'):
        blobs.put('bob', digest, 8, BytesIO(b'00000000'))
    blobs.put('bob', digest, 8, BytesIO(data))
    with blobs.open('bob', digest) as (stream,_):
        assert stream.read() == data
    with pytest.raises(WorkloadError, match='capacity'):
        blobs.put('alice', hashlib.sha256(b'x').hexdigest(), 1, BytesIO(b'x'))


def test_failed_upload_releases_quota_and_recovery_removes_orphans(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'})
    blobs = BlobStore(store, tmp_path/'objects', max_bytes=1)
    digest = hashlib.sha256(b'x').hexdigest()
    with pytest.raises(WorkloadError, match='incomplete'):
        blobs.put('alice', digest, 1, BytesIO(b''))
    assert list((tmp_path/'objects').iterdir()) == []
    (tmp_path/'objects'/'.upload-orphan').write_bytes(b'x')
    blobs.recover()
    assert list((tmp_path/'objects').iterdir()) == []
    blobs.put('alice', digest, 1, BytesIO(b'x'))


def test_referenced_inputs_survive_retention(tmp_path):
    now = [1000.0]
    store = WorkloadStore(tmp_path/'jobs.db', handlers={'test.v1'}, clock=lambda: now[0])
    blobs = BlobStore(store, tmp_path/'objects', retention_seconds=10)
    data = b'inputs'
    digest = hashlib.sha256(data).hexdigest()
    component = b'component'
    component_digest = hashlib.sha256(component).hexdigest()
    blobs.put('alice', digest, len(data), BytesIO(data))
    blobs.put('alice', component_digest, len(component), BytesIO(component))
    store.submit('alice', dict(version=2,key='one',handler='test.v1',input_digest=digest,
        input_objects=[{'name':'component','digest':component_digest,'size':len(component)}],need={'cpu':1}))
    now[0] += 20
    blobs.prune()
    with blobs.open('alice', digest) as (stream,_):
        assert stream.read() == data
    with blobs.open('alice', component_digest) as (stream,_):
        assert stream.read() == component
