"""Bounded source/artifact transfer over the same authenticated control plane."""
from __future__ import annotations

import logging
import os
from pathlib import Path
import tempfile
import time
from collections import deque
from urllib.parse import urlsplit

from .archive import file_digest
from .blobs import BlobStore
from .model import ArtifactTooLarge, WorkloadError
from .download import download_into  # noqa: F401  (re-exported for callers and tests)
from . import route_kinds  # noqa: F401  (registers the built-in route kinds)
from .routes import RouteDescriptor, RouteSet, Trail


class InputTransfer:
    """Object PUT/GET against the authority over a RouteSet.

    Without `routes`, the set is built from the classic arguments: `relay` is a client
    for a stateless edge forwarder (edge_forward.py) in front of the SAME authority,
    tried first (priority 0, metered) and skipped, with the reason logged, when its
    status reports the budget exhausted or it is unreachable; the authority (priority
    1, free) is always the last route. Pass `routes` (a RouteSet) to use any other
    set of routes; the classic arguments are then ignored.

    Both directions fail over mid-transfer: a download continues at the byte offset
    already on disk, an upload at the offset the authority already holds, so a route
    failure never restarts the object and never stores a byte twice. The route trail
    of the latest transfer is `last_trail`; counters are `routes.counters`.
    """

    def __init__(self, client, *, max_bytes=2*1024**3, relay=None, relay_key=None, relay_parallel=4,
                 routes=None, region=None):
        if (relay is None) != (relay_key is None):
            raise ValueError('relay and relay_key are configured together')
        self.client = client
        self.max_bytes = max_bytes
        self.relay = relay
        self.relay_key = relay_key
        self.relay_parallel = relay_parallel
        self.region = region
        self.peer = urlsplit(client.url).netloc
        if routes is None:
            built = []
            if relay is not None:
                built.append(route_kinds.HttpRoute(
                    RouteDescriptor('relay', 'edge_relay', relay.url, cost='metered', priority=0),
                    relay, edge_key=relay_key, parallel=relay_parallel))
            built.append(route_kinds.HttpRoute(
                RouteDescriptor('authority', 'http', client.url, cost='free', priority=1), client))
            routes = RouteSet(built)
        self.routes = routes
        self.last_trail = []
        self.recent_trails = deque(maxlen=32)

    def headers(self, assignment=None):
        result = {'Authorization': 'Bearer '+self.client.token}
        if assignment is not None:
            result.update({'X-Workload-Attempt': assignment['attempt_id'],
                           'X-Workload-Fence': str(assignment['fence']),
                           'X-Workload-Boot': assignment['boot']})
        return result

    def _finish(self, trail):
        self.last_trail = trail.as_list()
        self.recent_trails.append(self.last_trail)

    def _run(self, direction, size, attempt, on_failure=None):
        """Try routes best-first until one completes; `attempt(route, ctx)` moves bytes."""
        trail, last = Trail(), None
        for route in self.routes.candidates(direction, size, trail, peer=self.peer, region=self.region):
            ctx, started = {}, time.monotonic()
            if not route.slots.acquire(timeout=self.routes.policy.slot_wait_seconds):
                last = WorkloadError('route %s saturated' % route.name, 429)
                self.routes.record(route, self.peer, False, trail, error=last)
                continue
            try:
                result = attempt(route, ctx)
            except (OSError, ValueError, WorkloadError) as error:
                last = error
                self.routes.record(route, self.peer, False, trail, nbytes=ctx.get('moved', 0),
                                   seconds=time.monotonic()-started, error=error)
                if on_failure:
                    on_failure(route, error)
            else:
                self.routes.record(route, self.peer, True, trail, nbytes=ctx.get('moved', 0),
                                   seconds=time.monotonic()-started)
                self._finish(trail)
                return result
            finally:
                route.slots.release()
        self._finish(trail)
        raise self.routes.exhausted(trail, last)

    def put(self, source, *, assignment=None):
        source = Path(source)
        size = source.stat().st_size
        if size > self.max_bytes:
            raise WorkloadError('upload byte limit exceeded', 413)
        digest = file_digest(source)

        def attempt(route, ctx):
            ctx['headers'] = self.headers(assignment)
            return route.upload(source, digest, size, ctx)
        return self._run('put', size, attempt)

    def get(self, digest, destination, *, assignment=None):
        digest = BlobStore.digest(digest)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            if file_digest(destination) == digest:
                return destination
            raise WorkloadError('cached object has wrong digest', 409)
        fd, temporary = tempfile.mkstemp(prefix='.download-', dir=destination.parent)
        try:
            with os.fdopen(fd, 'r+b') as out:
                def attempt(route, ctx):
                    ctx.update(headers=self.headers(assignment), max_bytes=self.max_bytes)
                    out.seek(0, os.SEEK_END)  # resume where the previous route stopped
                    route.download(digest, out, ctx)
                self._run('get', None, attempt, self._reset_after_mismatch(out))
                out.flush()
                os.fsync(out.fileno())
            os.replace(temporary, destination)
            return destination
        finally:
            Path(temporary).unlink(missing_ok=True)

    @staticmethod
    def _reset_after_mismatch(out):
        def reset(route, error):
            # A verified-wrong object (409) means the prefix, possibly from another
            # route, cannot be trusted: the next route starts the object over.
            if isinstance(error, WorkloadError) and error.status == 409:
                out.seek(0)
                out.truncate()
                logging.warning('route %s produced unverifiable bytes (%s); next route restarts the object',
                                route.name, error)
        return reset
