"""The Coordinator seam — the one narrow interface livestack adapts to.

``polycore`` defines this Protocol; livestack ships a ``LivestackCoordinator`` that
implements it against the VRAM lease broker. ``polycore`` NEVER imports livestack
(dependency inversion), so the public servers stay standalone and livestack-free by
default.

The default :class:`LocalCoordinator` reproduces today's per-process discipline
(COLOAD + evict-all-on-idle), so swapping a server's embedded manager for
``polycore.ModelManager`` is behaviour-preserving.
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Coordinator(Protocol):
    """Policy seam for residency. Methods run on the GPU executor thread, with the
    manager's ``_guard`` held by the caller. Implementations drive the manager's
    ``_load``/``_evict`` primitives; they never touch the device directly."""

    def bind(self, manager) -> None:
        """Attach the local executor (ModelManager) this coordinator drives."""

    def acquire(self, name: str) -> object:
        """Make ``name`` resident and return its model, evicting per policy."""

    def idle_sweep(self) -> bool:
        """Evict per policy if idle past the timeout. Return whether anything was
        evicted."""

    def on_release_all(self, evicted: list[str]) -> None:
        """Hook after ``unload_now`` force-evicts everything (e.g. notify the broker)."""

    def on_evict_request(self, name: str) -> None:
        """Broker → 'please unload ``name``'. Local executor obliges. No-op locally."""

    def report_busy(self, name: str, busy: bool) -> None:
        """Surface a busy/idle edge for ``name`` (e.g. to the broker). No-op locally."""

    def on_degraded(self, name: str) -> None:
        """A resident unit failed its FUNCTIONAL health probe and was just
        evicted+reloaded locally. Heartbeat/busy-idle liveness can't see this
        (the process never died), so surface it to the broker for fleet health /
        reload accounting. No-op locally — the manager already did the reload."""


class LocalCoordinator:
    """Today's in-process discipline, factored out unchanged.

    ``coload=True``  : acquiring a unit does NOT evict the others (several resident).
    ``coload=False`` : acquiring one evicts any other (one-in-VRAM behaviour).
    Idle sweep evicts ALL resident units once idle past the timeout.
    """

    def __init__(self, coload: bool = True):
        self.coload = coload
        self.mgr = None

    def bind(self, manager) -> None:
        self.mgr = manager

    def acquire(self, name: str) -> object:
        m = self.mgr
        # The planner decides eviction (COLOAD vs one-in-VRAM); we execute it.
        evict, load = m._planner.plan_acquire(self.coload, name)
        for other in evict:
            m._evict(other)
        model = None
        for n in load:
            model = m._load(n)
        return model if load else m.units[name].model

    def idle_sweep(self) -> bool:
        m = self.mgr
        idle_for = time.monotonic() - m.last_used
        victims = m._planner.plan_idle_sweep(m.idle_seconds, idle_for)
        for name in victims:
            m._evict(name)
        return bool(victims)

    # Local mode has no broker; these are no-ops.
    def on_release_all(self, evicted: list[str]) -> None:
        return None

    def on_evict_request(self, name: str) -> None:
        if self.mgr and self.mgr._planner.is_resident(name):
            self.mgr._evict(name)

    def report_busy(self, name: str, busy: bool) -> None:
        return None

    def on_degraded(self, name: str) -> None:
        return None
