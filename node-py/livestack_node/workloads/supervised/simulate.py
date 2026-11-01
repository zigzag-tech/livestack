"""Fake-clock proof that a supervised handler stays within its declared max_silence.

The handler runs under enforce mode with a simulated backend: every slow ctx operation takes the time its
scenario entry says and ticks the way a real one would (exec and transfers tick per chunk; wait is silent).
If the handler's structure leaves a gap longer than the contract allows, SilenceBreach is raised."""
from __future__ import annotations

from .supervised import SilenceBreach  # noqa: F401  (re-exported for tests)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class SimBackend:
    def __init__(self, clock, scenario):
        self.clock, self.scenario = clock, scenario or {}
        self.calls = []

    def _advance(self, ctx, kind, chunked):
        spec = self.scenario.get(kind, {'duration_s': 1, 'chunk_gap_s': 1})
        self.calls.append(kind)
        remaining, gap = spec['duration_s'], spec.get('chunk_gap_s', 1)
        while remaining > 0:
            step = min(1, remaining) if not chunked else min(gap, remaining)
            self.clock.now += step
            remaining -= step
            if chunked:
                ctx.tick()
            else:
                ctx.flush()
            ctx.guard()

    def exec(self, ctx, cmd, args, **_):
        self._advance(ctx, 'exec', True)
        return {'code': 0, 'tail': b''}

    def wait(self, ctx, awaitable, label):
        self._advance(ctx, 'wait', False)
        return awaitable() if callable(awaitable) else None

    def download(self, ctx, url, dest):
        self._advance(ctx, 'download', True)
        return 0

    def upload(self, ctx, url, path):
        self._advance(ctx, 'upload', True)
        return 200


def prove(run, scenario, *args, **kwargs):
    """Run `run` (a supervised handler) in the simulated world. Returns the emitted documents; raises
    SilenceBreach if any gap exceeds the contract, AssertionError if it never emitted."""
    clock, emitted = FakeClock(), []
    result = run.rebuild(mode='enforce', clock=clock, backend=SimBackend(clock, scenario),
                         sink=emitted.append)(*args, **kwargs)
    assert emitted, 'a supervised handler must emit at least one document'
    return emitted, result
