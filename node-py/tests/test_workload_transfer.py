"""Real caller upload and worker download through the authenticated HTTP API."""
from threading import Thread
import urllib.error

import pytest

from livestack_node.workloads.archive import capture, unpack
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer


def test_remote_input_transfer_is_scoped_to_current_attempt(tmp_path):
    store = WorkloadStore(tmp_path/'authority/jobs.db', handlers={'test.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', 'a'*32, 'caller', ('test.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='win-wsl', host='win'),
    ])
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        caller = WorkloadClient(url, 'a'*32)
        worker = WorkloadClient(url, 'w'*32)
        root = tmp_path/'source'
        root.mkdir()
        (root/'hello.py').write_text('print("captured")\n')
        bundle = tmp_path/'source.tar'
        capture(root, ['hello.py'], bundle)
        uploaded = InputTransfer(caller).put(bundle)
        job = caller.submit(dict(version=1,key='one',handler='test.v1',input_digest=uploaded['digest'],need={'cpu':1}))
        worker.request('worker/report', dict(boot='boot1', report=dict(
            capacity={'cpu':2},available={'cpu':2},labels={},handlers=['test.v1'],ready=True)))
        a = worker.request('worker/claim', {'boot':'boot1'})['assignment']
        received = InputTransfer(worker).get(uploaded['digest'], tmp_path/'worker/input.tar', assignment=a)
        unpack(received, tmp_path/'worker/source', uploaded['digest'])
        assert (tmp_path/'worker/source/hello.py').read_text() == 'print("captured")\n'
        caller.request('jobs/'+job['id']+'/cancel', {})
        with pytest.raises(urllib.error.HTTPError) as error:
            InputTransfer(worker).get(uploaded['digest'], tmp_path/'different/input.tar', assignment=a)
        assert error.value.code == 409
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
