"""Bound the threads and sockets admitted by the stdlib workload HTTP server."""
from threading import BoundedSemaphore


class BoundedRequests:
    max_connections = 32

    def configure_connections(self):
        self._connections = BoundedSemaphore(self.max_connections)

    def process_request(self, request, client_address):
        if not self._connections.acquire(blocking=False):
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
