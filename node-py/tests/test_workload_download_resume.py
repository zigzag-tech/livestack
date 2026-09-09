"""Real authority behind an HTTP fault proxy; the proxy owns no product state."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer
from livestack_node.workloads.download import download_into


@pytest.mark.parametrize('fault', ['disconnect', 'wrong-offset', 'corrupt-bytes', 'always-disconnect'])
def test_resume_through_real_authority_and_fault_proxy(tmp_path, fault):
    store = WorkloadStore(tmp_path/'authority.sqlite', handlers={'test.v1'})
    authority = WorkloadServer(('127.0.0.1', 0), store, [Principal('owner', 'a'*32, 'caller', ('test.v1',))])
    upstream = Thread(target=authority.serve_forever, daemon=True); upstream.start()
    ranges = []

    class FaultProxy(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_GET(self):
            ranges.append(self.headers.get('Range'))
            conn = http.client.HTTPConnection('127.0.0.1', authority.server_port, timeout=5)
            try:
                headers = dict(self.headers)
                headers.pop('X-Harmony-Block-Encoding', None)
                conn.request('GET', self.path, headers=headers)
                response = conn.getresponse()
                self.send_response(response.status)
                for key, value in response.getheaders():
                    if fault == 'wrong-offset' and key.lower() == 'content-range':
                        value = 'bytes 0-9/10'
                    self.send_header(key, value)
                self.end_headers()
                self.close_connection = True
                if len(ranges) == 1 or fault == 'always-disconnect':
                    self.wfile.write(response.read(65536))
                    return
                while chunk := response.read(65536):
                    if fault == 'corrupt-bytes':
                        chunk = bytes([chunk[0] ^ 1])+chunk[1:]
                    self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                conn.close()

    proxy = ThreadingHTTPServer(('127.0.0.1', 0), FaultProxy)
    proxy_thread = Thread(target=proxy.serve_forever, daemon=True); proxy_thread.start()
    try:
        content = bytes(range(256))*8192
        source = tmp_path/'input'; source.write_bytes(content)
        direct = WorkloadClient(f'http://127.0.0.1:{authority.server_port}', 'a'*32)
        receipt = InputTransfer(direct).put(source)
        client = WorkloadClient(f'http://127.0.0.1:{proxy.server_port}', 'a'*32)
        destination = tmp_path/'destination'
        if fault == 'disconnect':
            InputTransfer(client).get(receipt['digest'], destination)
            assert destination.read_bytes() == content
            assert ranges == [None, 'bytes=65536-4259839']
        elif fault in ('wrong-offset', 'corrupt-bytes'):
            message = 'range does not match' if fault == 'wrong-offset' else 'content does not match'
            with pytest.raises(WorkloadError, match=message):
                InputTransfer(client).get(receipt['digest'], destination)
            assert not destination.exists()
            assert not list(tmp_path.glob('.download-*'))
            assert len(ranges) == 2, 'identity errors are not retried'
        else:
            with destination.open('wb') as out, pytest.raises(WorkloadError, match='retry budget'):
                download_into(client, receipt['digest'], {'Authorization': 'Bearer '+client.token}, out,
                              4*1024*1024, max_failures=2)
            assert len(ranges) == 3, 'progress does not reset the total retry bound'
    finally:
        proxy.shutdown(); proxy_thread.join(5); proxy.server_close()
        authority.shutdown(); upstream.join(5); authority.server_close()
