"""Bound the threads and sockets admitted by the stdlib workload HTTP server."""
import logging
import socket
from threading import Condition
import time


class BoundedRequests:
    # Count bound on open connections (and handler threads). A server may
    # raise it per principal through connection_bound(); this is the share
    # every server keeps for short requests (object/CAS, verifier, callers).
    max_connections = 32
    # How long an arriving connection waits for an evicted idle slot to free.
    evict_wait_seconds = 1.0

    def configure_connections(self):
        self._slots = Condition()
        self._open = 0
        self._idle = {}  # kept-alive socket -> monotonic time it went idle

    def connection_bound(self):
        return self.max_connections

    def mark_idle(self, request):
        """A kept-alive connection is waiting for its next request: it may be
        closed to admit a new connection at the bound."""
        with self._slots:
            self._idle[request] = time.monotonic()

    def mark_busy(self, request):
        with self._slots:
            self._idle.pop(request, None)

    def _admit(self):
        """Take a slot. At the bound, close the longest-idle kept-alive
        connection (its client reconnects on its next request) and wait
        briefly for that slot; only busy connections can refuse a newcomer."""
        with self._slots:
            if self._open < self.connection_bound():
                self._open += 1
                return True
            if self._idle:
                victim = min(self._idle, key=self._idle.get)
                del self._idle[victim]
                logging.info('workload_idle_connection_evicted_at_bound: max_connections=%d',
                             self.connection_bound())
                try:
                    victim.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                if self._slots.wait_for(lambda: self._open < self.connection_bound(),
                                        self.evict_wait_seconds):
                    self._open += 1
                    return True
            return False

    def process_request(self, request, client_address):
        if not self._admit():
            # The peer sees only a reset/URLError; name the cause here.
            logging.warning('workload_connection_dropped_at_bound: peer=%s max_connections=%d',
                            client_address[0] if isinstance(client_address, tuple) else client_address,
                            self.connection_bound())
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._release(request)
            raise

    def _release(self, request):
        with self._slots:
            self._open -= 1
            self._idle.pop(request, None)
            self._slots.notify_all()

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release(request)
