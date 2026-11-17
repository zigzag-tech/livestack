"""A real upload headroom refusal must not prevent admitted input reads."""
from threading import Thread
import urllib.error

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.routes import RoutePolicy
from livestack_node.workloads.storage_bounds import HeadroomGuard, StorageBounds
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer


@pytest.mark.parametrize('actor', ['caller', 'worker'])
def test_upload_507_circuit_preserves_existing_input_download(tmp_path, actor):
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'probe.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', 'a'*32, 'caller', ('probe.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='test-worker', host='test-host'),
    ])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    caller = WorkloadClient(f'http://127.0.0.1:{server.server_port}', 'a'*32)
    worker = WorkloadClient(f'http://127.0.0.1:{server.server_port}', 'w'*32)
    try:
        source = tmp_path/'source.bin'
        source.write_bytes(b'authenticated source already retained at the authority')
        uploaded = InputTransfer(caller).put(source)
        caller.submit(dict(version=1, key='held-source', handler='probe.v1',
                           input_digest=uploaded['digest'], need={'cpu': 1}))
        worker.request('worker/report', dict(boot='boot1', report=dict(
            capacity={'cpu': 2}, available={'cpu': 2}, labels={},
            handlers=['probe.v1'], ready=True)))
        assignment = worker.request('worker/claim', {'boot': 'boot1'})['assignment']
        moved = InputTransfer(worker if actor == 'worker' else caller)
        moved.routes.policy = RoutePolicy(open_seconds=60)
        scope = {'assignment': assignment} if actor == 'worker' else {}

        # Real statvfs + a valid 100% headroom floor refuses writes without
        # filling any disk. The held job keeps its existing input referenced.
        bounds = StorageBounds.validate({'objects': {'headroom_fraction': 1.0}})
        server.blobs.replace_policy(HeadroomGuard(
            server.blobs.root, server.blobs.max_bytes, bounds), None, 256)
        artifact = tmp_path/'receipt.json'
        artifact.write_bytes(b'{"exit_code":0}\n')
        for _ in range(moved.routes.policy.failure_threshold):
            with pytest.raises((urllib.error.HTTPError, WorkloadError)) as refused:
                moved.put(artifact, **scope)
            error = refused.value
            assert getattr(error, 'code', getattr(error, 'status', None)) == 507
        assert any(row['state'] == 'open' for row in moved.routes.snapshot().values())

        received = moved.get(uploaded['digest'], tmp_path/'received.bin', **scope)
        assert received.read_bytes() == source.read_bytes()
        assert moved.last_trail[-1]['outcome'] == 'ok'

        # A healthy GET must not reset or bypass the still-refused PUT circuit.
        with pytest.raises(WorkloadError, match='circuit open'):
            moved.put(artifact, **scope)
        states = sorted(row['state'] for row in moved.routes.snapshot().values())
        assert states == ['closed', 'open']
    finally:
        caller.close()
        worker.close()
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
