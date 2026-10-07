"""Route selection for object movement: descriptors, health, ordering, trail.

Plain Python, no third-party dependency (pydantic is not on every worker host).
Nothing here knows an application, a handler or a region name: a route is a
declarative descriptor plus a small `Route` implementation registered under a
`kind`. See node-py/docs/transport-routes.md for the principles and the guide to
adding a kind.

A `RouteSet` answers one question — "which route next, given what we know?" —
and records what each attempt cost. It does NOT move bytes; the caller (see
transfer.py) owns the transfer loop, so resume-at-offset lives next to the data.
"""
from __future__ import annotations

import logging
import threading
import time

from .model import WorkloadError

COSTS = ('free', 'metered', 'expensive')          # ascending
DIRECTIONS = ('get', 'put')
EXPENSIVE_POLICIES = ('never', 'last_resort', 'allow')
MAX_ROUTES = 16
TRAIL_LIMIT = 64


def _number(value, label, low, high, integer=False):
    kind = int if integer else (int, float)
    if isinstance(value, bool) or not isinstance(value, kind) or not low <= value <= high:
        raise ValueError('%s must be a number from %s to %s' % (label, low, high))
    return value


class RouteDescriptor:
    """Declarative, validated description of one candidate route.

    `priority` is operator intent (lower is tried first). `cost` is a class, not a
    price: free < metered < expensive. `budget_bytes` is advisory for routes that
    enforce their own budget remotely (the edge relay); `constraints` narrow when
    the route may be used at all."""
    FIELDS = {'name', 'kind', 'endpoint', 'cost', 'priority', 'budget_bytes', 'constraints', 'options'}
    CONSTRAINTS = {'directions', 'min_bytes', 'max_bytes', 'regions', 'max_inflight'}

    def __init__(self, name, kind, endpoint, *, cost='free', priority=0, budget_bytes=None, constraints=None, options=None):
        if not isinstance(name, str) or not name or len(name) > 64:
            raise ValueError('route name must be 1..64 characters')
        if not isinstance(kind, str) or not kind:
            raise ValueError('route %s needs a kind' % name)
        if not isinstance(endpoint, str) or not endpoint:
            raise ValueError('route %s needs an endpoint' % name)
        if cost not in COSTS:
            raise ValueError('route %s cost must be one of %s' % (name, COSTS))
        constraints = dict(constraints or {})
        if set(constraints) - self.CONSTRAINTS:
            raise ValueError('route %s has unknown constraints %s' % (name, sorted(set(constraints)-self.CONSTRAINTS)))
        directions = tuple(constraints.get('directions', DIRECTIONS))
        if not directions or set(directions) - set(DIRECTIONS):
            raise ValueError('route %s directions must be a subset of %s' % (name, DIRECTIONS))
        regions = constraints.get('regions')
        if regions is not None and (not isinstance(regions, (list, tuple)) or not all(isinstance(r, str) for r in regions)):
            raise ValueError('route %s regions must be a list of strings' % name)
        self.name, self.kind, self.endpoint, self.cost = name, kind, endpoint, cost
        self.priority = _number(priority, 'priority', -1000, 1000, True)
        self.budget_bytes = None if budget_bytes is None else _number(budget_bytes, 'budget_bytes', 1, 10**15, True)
        self.directions = directions
        self.min_bytes = _number(constraints.get('min_bytes', 0), 'min_bytes', 0, 10**15, True)
        self.max_bytes = (None if constraints.get('max_bytes') is None
                          else _number(constraints['max_bytes'], 'max_bytes', 1, 10**15, True))
        self.regions = None if regions is None else tuple(regions)
        self.max_inflight = _number(constraints.get('max_inflight', 4), 'max_inflight', 1, 64, True)
        self.options = dict(options or {})

    @classmethod
    def from_config(cls, raw):
        if not isinstance(raw, dict) or set(raw) - cls.FIELDS:
            raise ValueError('a route is an object with fields %s' % sorted(cls.FIELDS))
        missing = {'name', 'kind', 'endpoint'} - set(raw)
        if missing:
            raise ValueError('route is missing %s' % sorted(missing))
        return cls(**raw)

    def permits(self, direction, size, region):
        """None when this route may carry the transfer, else the named reason."""
        if direction not in self.directions:
            return 'direction %s not allowed' % direction
        if size is not None and size < self.min_bytes:
            return 'object below min_bytes'
        if size is not None and self.max_bytes is not None and size > self.max_bytes:
            return 'object above max_bytes'
        if self.regions is not None and region not in self.regions:
            return 'region %s not allowed' % region
        return None


class RoutePolicy:
    """Schema-validated knobs shared by every route in a set."""
    FIELDS = {'expensive', 'failure_threshold', 'open_seconds', 'max_open_seconds', 'stale_seconds',
              'ewma_alpha', 'degraded_below', 'slot_wait_seconds'}

    def __init__(self, *, expensive='last_resort', failure_threshold=3, open_seconds=5.0, max_open_seconds=300.0,
                 stale_seconds=600.0, ewma_alpha=0.3, degraded_below=0.5, slot_wait_seconds=60.0):
        if expensive not in EXPENSIVE_POLICIES:
            raise ValueError('expensive must be one of %s' % (EXPENSIVE_POLICIES,))
        self.expensive = expensive
        self.failure_threshold = _number(failure_threshold, 'failure_threshold', 1, 100, True)
        self.open_seconds = _number(open_seconds, 'open_seconds', 0, 3600)
        self.max_open_seconds = _number(max_open_seconds, 'max_open_seconds', self.open_seconds, 86400)
        self.stale_seconds = _number(stale_seconds, 'stale_seconds', 1, 86400*7)
        self.ewma_alpha = _number(ewma_alpha, 'ewma_alpha', 0.01, 1)
        self.degraded_below = _number(degraded_below, 'degraded_below', 0, 1)
        self.slot_wait_seconds = _number(slot_wait_seconds, 'slot_wait_seconds', 0, 3600)

    @classmethod
    def from_config(cls, raw):
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw) - cls.FIELDS:
            raise ValueError('policy is an object with fields %s' % sorted(cls.FIELDS))
        return cls(**raw)


class Health:
    """Observed behaviour of one route toward one peer: EWMAs plus a circuit breaker."""

    def __init__(self, policy, clock):
        self.policy, self.clock = policy, clock
        self.ok = 1.0                  # EWMA success rate
        self.bps = None                # EWMA throughput, bytes/second
        self.latency = None            # EWMA seconds to first byte / ack
        self.failures = 0              # consecutive
        self.state = 'closed'          # closed | open | half_open
        self.open_until, self.backoff = 0.0, policy.open_seconds
        self.probing = False
        self.updated = clock()

    def _forget_if_stale(self, now):
        if now-self.updated > self.policy.stale_seconds and self.state == 'closed':
            self.ok, self.bps, self.latency, self.failures = 1.0, None, None, 0

    def degraded(self):
        self._forget_if_stale(self.clock())
        return self.ok < self.policy.degraded_below

    def admit(self):
        """None when a transfer may start on this route, else the named reason."""
        now = self.clock()
        self._forget_if_stale(now)
        if self.state == 'closed':
            return None
        if self.state == 'open':
            if now < self.open_until:
                return 'circuit open for another %.0fs' % (self.open_until-now)
            self.state, self.probing = 'half_open', False
        if self.probing:
            return 'circuit half-open, trial already in flight'
        self.probing = True
        return None

    def record(self, ok, nbytes=0, seconds=0.0):
        a, now = self.policy.ewma_alpha, self.clock()
        self.updated, self.probing = now, False
        self.ok += a*((1.0 if ok else 0.0)-self.ok)
        if ok:
            self.failures, self.state, self.backoff = 0, 'closed', self.policy.open_seconds
            if nbytes and seconds > 0:
                rate = nbytes/seconds
                self.bps = rate if self.bps is None else self.bps+a*(rate-self.bps)
            if seconds > 0:
                self.latency = seconds if self.latency is None else self.latency+a*(seconds-self.latency)
            return
        self.failures += 1
        if self.state == 'half_open' or self.failures >= self.policy.failure_threshold:
            self.state, self.open_until = 'open', now+self.backoff
            self.backoff = min(self.backoff*2, self.policy.max_open_seconds)


class Trail:
    """Why each route was tried or abandoned. Bounded; rendered into logs and receipts."""

    def __init__(self):
        self.events = []

    def add(self, route, outcome, reason='', **fields):
        if len(self.events) < TRAIL_LIMIT:
            self.events.append(dict(route=route, outcome=outcome, reason=str(reason)[:200], **fields))

    def as_list(self):
        return list(self.events)

    def render(self):
        return '; '.join('%s %s%s' % (e['route'], e['outcome'], (' (%s)' % e['reason']) if e['reason'] else '')
                         for e in self.events)


class Route:
    """One way to move an object. Implement the direction(s) the descriptor allows.

    Contract: methods raise on failure (any exception is a route failure) and
    return only after the bytes are verified/acknowledged. A route never retries
    across routes — the set does — but may retry inside itself within a bound.
    """

    def __init__(self, descriptor):
        self.descriptor = descriptor
        self.name = descriptor.name
        self.slots = threading.BoundedSemaphore(descriptor.max_inflight)

    def unavailable(self):
        """Cheap pre-flight. None when usable, else the named reason (not a health failure)."""
        return None

    def upload(self, source, digest, size, ctx):          # pragma: no cover - interface
        raise NotImplementedError('%s does not upload' % self.descriptor.kind)

    def download(self, digest, out, ctx):                  # pragma: no cover - interface
        raise NotImplementedError('%s does not download' % self.descriptor.kind)


_KINDS = {}


def register_kind(kind, factory):
    """`factory(descriptor, **context) -> Route`. Re-registering a kind is a bug."""
    if kind in _KINDS:
        raise ValueError('route kind %s is already registered' % kind)
    _KINDS[kind] = factory


def build_route(descriptor, **context):
    if descriptor.kind not in _KINDS:
        raise ValueError('unknown route kind %s (registered: %s)' % (descriptor.kind, sorted(_KINDS)))
    return _KINDS[descriptor.kind](descriptor, **context)


class RouteSet:
    def __init__(self, routes, policy=None, *, clock=time.monotonic):
        routes = list(routes)
        if not 1 <= len(routes) <= MAX_ROUTES:
            raise ValueError('a route set holds 1..%d routes' % MAX_ROUTES)
        if len({r.name for r in routes}) != len(routes):
            raise ValueError('route names must be unique')
        self.routes, self.policy, self.clock = routes, policy or RoutePolicy(), clock
        self._health, self._lock = {}, threading.Lock()
        self._skipped = {}
        self.counters = {}   # (route, outcome) -> n; bounded by routes x outcomes

    @classmethod
    def from_config(cls, config, **context):
        if not isinstance(config, dict) or set(config) - {'policy', 'routes'}:
            raise ValueError('route config is an object with policy and routes')
        descriptors = [RouteDescriptor.from_config(r) for r in config.get('routes', [])]
        return cls([build_route(d, **context) for d in descriptors], RoutePolicy.from_config(config.get('policy')))

    def health(self, route, peer):
        with self._lock:
            key = (route.name, peer)
            if key not in self._health:
                self._health[key] = Health(self.policy, self.clock)
            return self._health[key]

    def count(self, route, outcome):
        with self._lock:
            self.counters[(route.name, outcome)] = self.counters.get((route.name, outcome), 0)+1

    def _order(self, pool, peer):
        cost = {c: i for i, c in enumerate(COSTS)}
        def key(item):
            index, route = item
            health = self.health(route, peer)
            return (health.degraded(), route.descriptor.priority, cost[route.descriptor.cost],
                    -(health.bps or 0.0), index)
        return [route for _index, route in sorted(enumerate(pool), key=key)]

    def candidates(self, direction, size, trail, *, peer='default', region=None):
        """Yield routes best-first, one at a time, re-ranked after each outcome.

        The caller MUST call `record()` after every yielded route (a half-open
        circuit holds its single trial until then). Each route is yielded at most
        once per call, so failover is bounded by the set size."""
        tried = set()
        while True:
            pool, expensive = [], []
            for route in self.routes:
                if route.name in tried:
                    continue
                reason = route.descriptor.permits(direction, size, region)
                if reason:
                    tried.add(route.name)
                    trail.add(route.name, 'ineligible', reason)
                    continue
                if route.descriptor.cost == 'expensive':
                    if self.policy.expensive == 'never':
                        tried.add(route.name)
                        trail.add(route.name, 'ineligible', 'expensive routes are not allowed')
                        continue
                    (expensive if self.policy.expensive == 'last_resort' else pool).append(route)
                else:
                    pool.append(route)
            for route in self._order(pool or expensive, peer):
                reason = route.unavailable()
                if reason is None:
                    reason = self.health(route, peer).admit()
                    if reason is None:
                        tried.add(route.name)
                        yield route
                        break
                else:
                    # Unavailable is a decision, not a failure: it must not poison health.
                    if self._skipped.get(route.name) != reason:
                        logging.warning('route %s skipped (%s); trying the next route', route.name, reason)
                    self._skipped[route.name] = reason
                tried.add(route.name)
                trail.add(route.name, 'skipped', reason)
                self.count(route, 'skipped')
                break
            else:
                return

    def record(self, route, peer, ok, trail, *, nbytes=0, seconds=0.0, error=None):
        self.health(route, peer).record(ok, nbytes, seconds)
        outcome = 'ok' if ok else 'failed'
        self.count(route, outcome)
        trail.add(route.name, outcome, '' if ok else '%s: %s' % (type(error).__name__, error), bytes=nbytes,
                  seconds=round(seconds, 3))
        if not ok:
            logging.warning('route %s abandoned after %d bytes: %s: %s', route.name, nbytes, type(error).__name__, error)

    def snapshot(self):
        """Operator view: per route+peer health, no secrets."""
        with self._lock:
            return {'%s@%s' % key: dict(state=h.state, ok=round(h.ok, 3), bps=h.bps, failures=h.failures)
                    for key, h in self._health.items()}

    def exhausted(self, trail, error):
        """The loud end of a transfer: name every route and why it was abandoned."""
        logging.error('all routes failed: %s', trail.render())
        if error is None:
            error = WorkloadError('no usable route: '+trail.render(), 503)
        try:
            error.route_trail = trail.as_list()
        except AttributeError:
            pass
        return error
