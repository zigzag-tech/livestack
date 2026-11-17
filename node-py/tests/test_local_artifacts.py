"""Real HTTP, SQLite and private files: no remote copy is needed for local results."""
import hashlib
import time
from io import BytesIO
from pathlib import Path
from threading import Thread

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.local_artifacts import LocalArtifactStore
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer


@pytest.fixture
def api(tmp_path):
    now = [time.time()]
    store = WorkloadStore(tmp_path/'authority.db', handlers={'build.v1'}, clock=lambda: now[0])
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', 'a'*32, 'caller', ('build.v1',)),
        Principal('other', 'b'*32, 'caller', ('build.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='w1', host='laptop'),
    ])
    source = b'captured source'
    digest = hashlib.sha256(source).hexdigest()
    server.blobs.put('owner', digest, len(source), BytesIO(source))
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    address = f'http://127.0.0.1:{server.server_port}'
    clients = [WorkloadClient(address, token) for token in ('a'*32, 'b'*32, 'w'*32)]
    caller, other, worker = clients
    worker.request('worker/report', dict(boot='boot', cleaned=[], report=dict(
        capacity={'cpu': 2}, available={'cpu': 2}, handlers=['build.v1'], labels={},
        ready=True, local_artifacts=True)))
    caller.submit(dict(version=1, key='build', handler='build.v1', input_digest=digest,
        need={'cpu': 1}, resource_profile='linux-debug',
        delivery=dict(consumer_host='laptop', expected_output_bytes=2048)))
    assignment = worker.request('worker/claim', dict(boot='boot'))['assignment']
    root = tmp_path/'cache'
    cache = LocalArtifactStore(root, 'laptop', 'build-cache', ttl_seconds=60, clock=lambda: now[0])
    try:
        yield dict(caller=caller, other=other, worker=worker, assignment=assignment,
                   cache=cache, now=now, server=server, root=root, store=store, tmp=tmp_path)
    finally:
        for client in clients: client.close()
        server.shutdown(); thread.join(timeout=5); server.server_close()


def produce(api, *, complete=True):
    source = api['tmp']/'binary'; data = b'actual artifact bytes'*128; source.write_bytes(data)
    cache, assignment = api['cache'], api['assignment']
    ref = cache.put(source, assignment)
    ref = api['worker'].request('worker/local-artifacts', dict(boot='boot',
        attempt_id=assignment['attempt_id'], fence=assignment['fence'], artifact=dict(
            name='binary', digest=ref['digest'], size=ref['size'], store=ref['store'],
            handle=ref['handle'], ttl_seconds=60)))
    cache.bind(ref)
    if complete:
        api['worker'].request('worker/complete', dict(boot='boot', attempt_id=assignment['attempt_id'],
            fence=assignment['fence'], input_digest=assignment['spec']['input_digest'],
            outcome='succeeded', result=dict(exit_code=0, resources=dict(oom_kill=0, pids_max_events=0),
                artifacts=[dict(name='binary', digest=ref['digest'], size=ref['size'], local=ref)])))
    return ref, data


def test_verified_local_result_is_accepted_without_authority_binary_bytes(api):
    reference, data = produce(api)
    assignment = api['assignment']
    with pytest.raises(WorkloadError, match='not found'):
        with api['server'].blobs.open('owner', reference['digest']): pass
    config = dict(root=str(api['root']), host='laptop', store='build-cache')
    destination = api['tmp']/'installed-binary'
    accepted = InputTransfer(api['caller'], local_artifacts=config).get(
        reference['digest'], destination, job_id=assignment['job_id'])
    assert accepted.read_bytes() == data


def test_same_host_copy_checks_real_bytes_and_identity(api):
    reference, data = produce(api)
    destination = api['tmp']/'installed'
    assert api['cache'].copy(reference, destination).read_bytes() == data
    altered = dict(reference, owner='other')
    with pytest.raises(WorkloadError, match='binding differs'):
        api['cache'].copy(altered, api['tmp']/'foreign')
    (api['root']/'objects'/reference['digest']).write_bytes(b'x'*len(data))
    with pytest.raises(WorkloadError, match='digest differs'):
        api['cache'].copy(reference, api['tmp']/'tampered')
    assert not (api['tmp']/'tampered').exists()


def test_foreign_owner_and_fenced_producer_cannot_use_local_grants(api):
    reference, _ = produce(api)
    a = api['assignment']
    with pytest.raises(WorkloadError, match='not found'):
        api['other'].request('jobs/'+a['job_id']+'/local-artifacts/'+reference['digest'])
    with pytest.raises(WorkloadError, match='authorized'):
        api['worker'].request('worker/local-artifacts', dict(boot='boot', attempt_id=a['attempt_id'],
            fence=a['fence'], artifact=dict(name='binary', digest=reference['digest'], size=reference['size'],
                store=reference['store'], handle=reference['handle'], ttl_seconds=60)))


@pytest.mark.parametrize('cause', ['expiry', 'cancellation'])
def test_expiry_and_cancellation_preserve_boundaries(api, cause):
    reference, _ = produce(api, complete=cause == 'expiry')
    a = api['assignment']
    if cause == 'cancellation':
        api['caller'].cancel(a['job_id'])
        with pytest.raises(WorkloadError, match='not succeeded'):
            api['caller'].request('jobs/'+a['job_id']+'/local-artifacts/'+reference['digest'])
    api['now'][0] += 61
    if cause == 'expiry':
        with pytest.raises(WorkloadError, match='expired'):
            api['caller'].request('jobs/'+a['job_id']+'/local-artifacts/'+reference['digest'])
    api['cache'].sweep()
    assert not list((api['root']/'objects').iterdir())
    assert not list((api['root']/'handles').iterdir())


def test_cache_refuses_size_overflow_and_symlink_objects(api):
    source = api['tmp']/'too-big'; source.write_bytes(b'12345')
    cache = LocalArtifactStore(api['tmp']/'tiny', 'laptop', 'tiny', max_bytes=4, clock=lambda: 1000)
    with pytest.raises(WorkloadError, match='capacity'):
        cache.put(source, api['assignment'])
    reference, _ = produce(api)
    path = api['root']/'objects'/reference['digest']; path.unlink(); path.symlink_to(source)
    with pytest.raises(WorkloadError, match='invalid local artifact object'):
        api['cache'].sweep()


def test_explicit_job_handoff_is_read_only_and_expires(api):
    reference, data = produce(api)
    job_id = api['assignment']['job_id']
    api['caller'].request('jobs/'+job_id+'/share', dict(recipient='other', ttl_seconds=60))
    assert api['other'].get(job_id)['state'] == 'succeeded'
    config = dict(root=str(api['root']), host='laptop', store='build-cache')
    assert InputTransfer(api['other'], local_artifacts=config).get(reference['digest'],
        api['tmp']/'shared', job_id=job_id).read_bytes() == data
    with pytest.raises(WorkloadError, match='not found'):
        api['other'].cancel(job_id)
    with pytest.raises(WorkloadError, match='not found'):
        api['other'].request('jobs/'+job_id+'/share', dict(recipient='owner'))
    api['now'][0] += 61
    with pytest.raises(WorkloadError, match='not found'):
        api['other'].get(job_id)


def test_explicit_source_handoff_does_not_transfer_ownership(api):
    digest = api['assignment']['spec']['input_digest']
    api['caller'].request('shares/objects', dict(recipient='other', digests=[digest], ttl_seconds=60))
    with api['server'].blobs.open('other', digest) as (stream, size):
        assert stream.read() == b'captured source'
    with pytest.raises(WorkloadError, match='not owned'):
        api['other'].request('shares/objects', dict(recipient='owner', digests=[digest]))
    api['now'][0] += 61
    with pytest.raises(WorkloadError, match='not found'):
        with api['server'].blobs.open('other', digest): pass
