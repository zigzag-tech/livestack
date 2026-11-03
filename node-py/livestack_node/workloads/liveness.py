"""Object-transfer liveness: stall window, throughput-scaled ceiling, adaptive block size.

One rule, specified by openspec `object-transfer-liveness` and pinned by the test vectors in
tests/fixtures/transfer_liveness_vectors.json. The ZZOPS Node client (zzops
packages/harmony) implements the SAME rule in JavaScript and runs a copy of the same vectors,
because a Node process cannot import this module; change the vectors first, then both sides.

The rule:
  * an attempt (one ranged request) is abandoned as `stalled` when no byte has moved for
    `stall_seconds`, and as `duration` when it has run `ceiling(nbytes)` =
    max(minimum, nbytes / floor_rate). A fixed whole-response clock never bounds progress.
  * the requested range follows measured throughput: after a full block, the EWMA (alpha 0.5)
    of its goodput decides the next size. At or above `slow_rate` the size doubles back toward
    `max_block`; below it, the size is what the path moves in half a stall window, rounded down
    to 64 KiB and clamped to [min_block, max_block]. A failed attempt halves the size.
"""
from __future__ import annotations

KIB = 1024
MIB = 1024*KIB
QUANTUM = 64*KIB
EWMA_ALPHA = 0.5

DEFAULTS = dict(stall_seconds=20.0, ceiling_minimum=60.0, floor_rate=64*KIB, slow_rate=256*KIB,
                max_block=4*MIB, min_block=256*KIB)


class StallError(TimeoutError):
    """No byte moved for the stall window. A TimeoutError so every existing handler still retries it."""


class Liveness:
    """Validated constants; per-route overrides come from a route descriptor's options."""

    def __init__(self, **overrides):
        unknown = set(overrides)-set(DEFAULTS)
        if unknown:
            raise ValueError('unknown liveness options %s' % sorted(unknown))
        values = {**DEFAULTS, **overrides}
        for key, value in values.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError('liveness %s must be a positive number' % key)
        if values['min_block'] > values['max_block']:
            raise ValueError('liveness min_block exceeds max_block')
        self.__dict__.update(values)
        self.max_block, self.min_block = int(self.max_block), int(self.min_block)

    @classmethod
    def from_options(cls, options):
        return cls(**{key: options[key] for key in DEFAULTS if key in options})

    def ceiling(self, nbytes, minimum=None):
        return max(self.ceiling_minimum if minimum is None else minimum, nbytes/self.floor_rate)

    def verdict(self, idle, elapsed, nbytes):
        """'ok' | 'stalled' | 'duration' for an attempt of `nbytes` that has run `elapsed`
        seconds with `idle` seconds since its last byte. Stalled outranks duration."""
        if idle >= self.stall_seconds:
            return 'stalled'
        return 'duration' if elapsed >= self.ceiling(nbytes) else 'ok'


class BlockSizer:
    """The requested range size for one transfer on one route."""

    def __init__(self, liveness):
        self.liveness = liveness
        self.block = liveness.max_block
        self.rate = None
        self.events = dict(blocks=0, stalls=0, ceilings=0, shrinks=0)
        self.smallest = self.block

    def ok(self, nbytes, seconds, requested):
        """A block finished. Short blocks (the object's tail, a smaller block the authority chose)
        say nothing about the path and are ignored."""
        self.events['blocks'] += 1
        if nbytes < requested or seconds <= 0:
            return
        rate = nbytes/seconds
        self.rate = rate if self.rate is None else EWMA_ALPHA*self.rate+(1-EWMA_ALPHA)*rate
        live = self.liveness
        if self.rate >= live.slow_rate:
            self.block = min(live.max_block, self.block*2)
        else:
            quanta = int(self.rate*live.stall_seconds/2/QUANTUM)  # same operation order as the JS twin
            self.block = max(live.min_block, min(live.max_block, quanta*QUANTUM))
        self.smallest = min(self.smallest, self.block)

    def failed(self, cause=None):
        if cause == 'stalled':
            self.events['stalls'] += 1
        elif cause == 'duration':
            self.events['ceilings'] += 1
        self.block = max(self.liveness.min_block, self.block//2)
        self.smallest = min(self.smallest, self.block)

    def detail(self):
        """What the route trail records: chosen block size, stall events."""
        return dict(block_bytes=self.block, min_block_bytes=self.smallest, **self.events,
                    rate_bps=None if self.rate is None else int(self.rate))
