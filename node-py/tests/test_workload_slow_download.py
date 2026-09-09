"""A real slow HTTP reader must not time out a healthy progressing download."""
import http.client
import socket
import time
from threading import Thread

from livestack_node.workloads.http import Handler, Principal, WorkloadServer
from livestack_node.workloads.store import WorkloadStore
from livestack_node.workloads.client import WorkloadClient
from livestack_node.workloads.transfer import InputTransfer


def test_progressing_slow_reader_survives_bounded_socket_writes(tmp_path):
    class ShortWriteDeadline(Handler):
        def setup(self):
            super().setup()
            self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4096)
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            self.connection.settimeout(.3)

    store = WorkloadStore(tmp_path/'jobs.sqlite', handlers={'test.v1'})
    server = WorkloadServer(('127.0.0.1', 0), store, [Principal('owner', 'a'*32, 'caller', ('test.v1',))])
    server.RequestHandlerClass = ShortWriteDeadline
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        source = tmp_path/'input'; content = bytes(range(256))*8192; source.write_bytes(content)
        caller = WorkloadClient(f'http://127.0.0.1:{server.server_port}', 'a'*32)
        uploaded = InputTransfer(caller).put(source)
        conn.connect()
        conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 16384)
        conn.request('GET', '/v1/workloads/objects/'+uploaded['digest'], headers={'Authorization': 'Bearer '+'a'*32})
        response = conn.getresponse(); assert response.status == 200
        chunks = []
        while chunk := response.read(4096):
            chunks.append(chunk)
            time.sleep(.01)  # Continual ~400 KiB/s progress, never a .3s idle gap.
        assert b''.join(chunks) == content
    finally:
        conn.close(); server.shutdown(); thread.join(5); server.server_close()
