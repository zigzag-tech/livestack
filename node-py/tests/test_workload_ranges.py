"""Real HTTP/SQLite/CAS range responses and interrupted-stream framing."""
import hashlib
import http.client
from threading import Thread
import urllib.error
import urllib.request

import pytest

from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.transfer import InputTransfer


def test_ranges_preserve_content_and_authorization_and_never_append_error_json(tmp_path):
    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers={'test.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [
        Principal('owner', 'a'*32, 'caller', ('test.v1',)),
        Principal('other', 'b'*32, 'caller', ('test.v1',)),
        Principal('worker', 'w'*32, 'worker', worker='win-wsl', host='win'),
    ])
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        url = f'http://127.0.0.1:{server.server_port}'
        source = tmp_path/'source'
        content = bytes(range(256))*20
        source.write_bytes(content)
        receipt = InputTransfer(WorkloadClient(url, 'a'*32)).put(source)

        def get(range_value=None, token='a'*32):
            headers = {'Authorization': 'Bearer '+token}
            if range_value is not None:
                headers['Range'] = range_value
            return urllib.request.urlopen(urllib.request.Request(
                url+'/v1/workloads/objects/'+receipt['digest'], headers=headers), timeout=5)

        pieces = []
        for start, end in ((0, 999), (1000, 2999), (3000, len(content)-1)):
            with get(f'bytes={start}-{end}') as response:
                assert response.status == 206
                assert response.headers['Content-Range'] == f'bytes {start}-{end}/{len(content)}'
                assert response.headers['ETag'] == '"'+receipt['digest']+'"'
                assert int(response.headers['Content-Length']) == end-start+1
                pieces.append(response.read())
        assert b''.join(pieces) == content
        assert hashlib.sha256(b''.join(pieces)).hexdigest() == receipt['digest']
        with get('bytes=4000-') as response:
            assert response.read() == content[4000:]
        with get('bytes=5000-99999') as response:
            assert response.read() == content[5000:]
        for invalid in ('bytes=-10', 'bytes=0-1,3-4', 'bytes=5-1', 'bytes=999999-', 'invalid'):
            with pytest.raises(urllib.error.HTTPError) as error:
                get(invalid)
            assert error.value.code == 416
        with pytest.raises(urllib.error.HTTPError) as error:
            get('bytes=0-10', token='b'*32)
        assert error.value.code == 404
        with get() as response:
            assert response.status == 200
            assert response.read() == content
        caller, worker = WorkloadClient(url, 'a'*32), WorkloadClient(url, 'w'*32)
        job = caller.submit(dict(version=1, key='one', handler='test.v1', input_digest=receipt['digest'], need={'cpu': 1}))
        worker.request('worker/report', dict(boot='boot1', report=dict(
            capacity={'cpu': 2}, available={'cpu': 2}, labels={}, handlers=['test.v1'], ready=True)))
        assignment = worker.request('worker/claim', {'boot': 'boot1'})['assignment']
        headers = InputTransfer(worker).headers(assignment)
        headers['Range'] = 'bytes=0-9'
        request = urllib.request.Request(url+'/v1/workloads/objects/'+receipt['digest'], headers=headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            assert response.read() == content[:10]
        caller.request('jobs/'+job['id']+'/cancel', {})
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=5)
        assert error.value.code == 409, 'each resumed range rechecks the current attempt fence'
        # Force a real file read to end early after successful response headers.
        # An HTTP error appended here can satisfy Content-Length with corrupt
        # bytes and masquerade as a digest mismatch rather than a short stream.
        (server.blobs.root/receipt['digest']).write_bytes(content[:10])
        with get() as response:
            with pytest.raises(http.client.IncompleteRead) as error:
                response.read()
            assert error.value.partial == content[:10]
    finally:
        server.shutdown(); thread.join(5); server.server_close()
