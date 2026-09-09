"""Real authenticated CAS transfers with an encoded-block fault proxy."""
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.http import Principal, WorkloadServer
from livestack_node.workloads.model import WorkloadError
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.transfer import InputTransfer
from livestack_node.workloads.block_codec import HEADER


@pytest.mark.parametrize('fault', ['disconnect', 'corrupt'])
def test_encoded_blocks_preserve_original_digest_and_resume(tmp_path, fault):
    store = WorkloadStore(tmp_path/'authority.sqlite', handlers=['test.v1'])
    authority = WorkloadServer(('127.0.0.1', 0), store, [Principal('owner', 'a'*32, 'caller', ('test.v1',))])
    upstream = Thread(target=authority.serve_forever, daemon=True); upstream.start()
    observations = []

    class Proxy(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'
        def log_message(self, *args):
            pass
        def do_GET(self):
            connection = http.client.HTTPConnection('127.0.0.1', authority.server_port, timeout=5)
            try:
                connection.request('GET', self.path, headers=dict(self.headers))
                response = connection.getresponse()
                observations.append((self.headers.get('Range'), response.getheader(HEADER), int(response.getheader('Content-Length'))))
                self.send_response(response.status)
                for k, v in response.getheaders(): self.send_header(k, v)
                self.end_headers(); self.close_connection = True
                data = response.read()
                if fault == 'disconnect' and len(observations) == 1:
                    self.wfile.write(data[:20]); return
                if fault == 'corrupt': data = data[:-1]+bytes([data[-1]^1])
                self.wfile.write(data)
            finally:
                connection.close()

    proxy = ThreadingHTTPServer(('127.0.0.1', 0), Proxy)
    thread = Thread(target=proxy.serve_forever, daemon=True); thread.start()
    try:
        content = b'captured immutable source\n'*400000
        source = tmp_path/'source'; source.write_bytes(content)
        direct = WorkloadClient(f'http://127.0.0.1:{authority.server_port}', 'a'*32)
        receipt = InputTransfer(direct).put(source)
        remote = InputTransfer(WorkloadClient(f'http://127.0.0.1:{proxy.server_port}', 'a'*32))
        if fault == 'disconnect':
            remote.get(receipt['digest'], tmp_path/'received')
            assert (tmp_path/'received').read_bytes() == content
            assert observations[0][0] is None
            assert observations[1][0] == 'bytes=0-4194303', 'an incomplete encoded block is retried from its original offset'
            assert all(row[1] == 'gzip' for row in observations)
            assert sum(row[2] for row in observations) < len(content)//10
        else:
            with pytest.raises(WorkloadError, match='compressed block'):
                remote.get(receipt['digest'], tmp_path/'received')
            assert len(observations) == 1, 'corrupt identity is not a transient retry'
            assert not (tmp_path/'received').exists()
            assert not list(tmp_path.glob('.download-*'))
        # Negotiation keeps zero-byte and already-compressed/random objects valid.
        for name, data in [('empty', b''), ('small', bytes(range(256)))]:
            path=tmp_path/name; path.write_bytes(data)
            uploaded=InputTransfer(direct).put(path)
            InputTransfer(direct).get(uploaded['digest'],tmp_path/(name+'-copy'))
            assert (tmp_path/(name+'-copy')).read_bytes()==data
    finally:
        proxy.shutdown();thread.join(5);proxy.server_close()
        authority.shutdown();upstream.join(5);authority.server_close()
