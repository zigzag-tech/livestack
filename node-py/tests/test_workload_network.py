"""The connection bound refuses a peer with only a reset; the server must say so."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import logging
import socket
from threading import Event, Thread

from livestack_node.workloads.network import BoundedRequests


def test_connection_dropped_at_bound_is_logged(caplog):
    release = Event()

    class Hold(BaseHTTPRequestHandler):
        def do_GET(self):
            release.wait(5)
            self.send_response(204)
            self.end_headers()

    class Server(BoundedRequests, ThreadingHTTPServer):
        daemon_threads = True
        max_connections = 1

        def __init__(self, *args):
            super().__init__(*args)
            self.configure_connections()

    server = Server(('127.0.0.1', 0), Hold)
    Thread(target=server.serve_forever, kwargs=dict(poll_interval=.05), daemon=True).start()
    try:
        with caplog.at_level(logging.WARNING), \
                socket.create_connection(server.server_address, timeout=5) as held:
            held.sendall(b'GET / HTTP/1.0\r\n\r\n')
            with socket.create_connection(server.server_address, timeout=5) as dropped:
                dropped.sendall(b'GET / HTTP/1.0\r\n\r\n')
                assert dropped.recv(1) == b''  # closed without an answer
            release.set()
            assert held.recv(12).startswith(b'HTTP/1.0 204')
        assert 'workload_connection_dropped_at_bound: peer=127.0.0.1 max_connections=1' in caplog.text
    finally:
        release.set()
        server.shutdown()
        server.server_close()
