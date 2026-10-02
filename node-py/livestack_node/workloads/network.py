"""Bound the threads and sockets admitted by the stdlib workload HTTP server."""
import logging
from threading import BoundedSemaphore


class BoundedRequests:
    max_connections = 32

    def configure_connections(self):
        self._connections = BoundedSemaphore(self.max_connections)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
            # The peer sees only a reset/URLError; name the cause here.
            logging.warning('workload_connection_dropped_at_bound: peer=%s max_connections=%d',
                            client_address[0] if isinstance(client_address, tuple) else client_address,
                            self.max_connections)
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._connections.release()
