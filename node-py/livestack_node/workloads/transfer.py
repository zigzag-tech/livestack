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
                 routes=None, region=None, cross_region=None, doors=None, authority_parallel=1):
        if (relay is None) != (relay_key is None):
            raise ValueError('relay and relay_key are configured together')
        if doors is not None and relay is not None:
            raise ValueError('configure either relay or doors, not both')
        self.client = client
        self.max_bytes = max_bytes
        self.relay = relay
        self.relay_key = relay_key
        self.relay_parallel = relay_parallel
        self.region = region
        self.peer = urlsplit(client.url).netloc
        if routes is None:
            built = []
            # `doors` is the ordered list form of `relay` (objects with name, client, key, parallel);
            # `relay` is a one-door list named `relay`. Every door is its own route, so one door's
            # outage skips that route alone and the transfer resumes on the next door.
            if relay is not None:
                doors = [dict(name='relay', client=relay, key=relay_key, parallel=relay_parallel)]
            for door in doors or ():
                built.append(route_kinds.HttpRoute(
                    RouteDescriptor(door['name'], 'edge_relay', door['client'].url, cost='metered', priority=0),
                    door['client'], edge_key=door['key'], parallel=door['parallel']))
            has_door = bool(built)
            # Behind a relay, the direct authority path is the fall-through a slow transfer is
            # attributed to (an operator-declared RouteSet may name it `mesh_derp` instead).
            built.append(route_kinds.HttpRoute(
                RouteDescriptor('authority', 'http', client.url, cost='free', priority=1,
                                options={'degraded_path': 'authority_fallthrough'} if has_door else None),
                client, parallel=authority_parallel))
            routes = RouteSet(built)
        self.routes = routes
        self.cross_region = cross_region
        self._warn_single_route()
        self.last_trail = []
        self.recent_trails = deque(maxlen=32)

    def health(self):
        """Operator view of object delivery. A cross-region worker (told so, or inferred from
        having any route besides the direct authority path) needs two independent doors;
        with fewer it is `single_route`, so one host's outage is a stop, not a slowdown."""
        doors = [r.name for r in self.routes.routes
                 if r.descriptor.kind != 'http' and r.descriptor.permits('get', None, self.region) is None]
        cross = self.cross_region if self.cross_region is not None else any(
            r.descriptor.kind != 'http' for r in self.routes.routes)
        return dict(cross_region=cross, doors=doors, single_route=bool(cross and len(doors) < 2))

    def _warn_single_route(self):
        health = self.health()
        if health['single_route']:
            logging.warning('single_route: cross-region object delivery has %d independent door(s) %s; '
                            'one host outage will force the throttled direct path',
                            len(health['doors']), health['doors'])

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
            fell_through = any(e['outcome'] in ('failed', 'skipped') for e in trail.events)
            if not route.slots.acquire(timeout=self.routes.policy.slot_wait_seconds):
                last = WorkloadError('route %s saturated' % route.name, 429)
                self.routes.record(route, self.peer, False, trail, error=last)
                continue
            try:
                result = attempt(route, ctx)
            except (OSError, ValueError, WorkloadError) as error:
                last = error
                self.routes.record(route, self.peer, False, trail, nbytes=ctx.get('moved', 0),
                                   seconds=time.monotonic()-started, error=error,
                                   detail=self._detail(route, ctx, fell_through))
                if on_failure:
                    on_failure(route, error)
            else:
                self.routes.record(route, self.peer, True, trail, nbytes=ctx.get('moved', 0),
                                   seconds=time.monotonic()-started, detail=self._detail(route, ctx, fell_through))
                self._finish(trail)
                return result
            finally:
                route.slots.release()
        self._finish(trail)
        raise self.routes.exhausted(trail, last)

    @staticmethod
    def _detail(route, ctx, fell_through):
        """Block size / stall events from the route, plus the declared path class of a fall-through."""
        detail = dict(ctx.get('detail') or {})
        path = route.descriptor.options.get('degraded_path')
        if fell_through and isinstance(path, str):
            detail['degraded_path'] = path[:32]
        return detail

    def put(self, source, *, assignment=None):
        source = Path(source)
        size = source.stat().st_size
        if size > self.max_bytes:
            raise ArtifactTooLarge(size, self.max_bytes)
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
