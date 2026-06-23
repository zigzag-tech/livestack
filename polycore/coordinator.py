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
        if name not in m._resident:
            if not self.coload:
                for other in list(m._resident):
                    if other != name:
                        m._evict(other)
            return m._load(name)
        return m.units[name].model

    def idle_sweep(self) -> bool:
        m = self.mgr
        if m.idle_seconds <= 0:
            return False
        if m._resident and (time.monotonic() - m.last_used) > m.idle_seconds:
            for name in list(m._resident):
                m._evict(name)
            return True
        return False

    # Local mode has no broker; these are no-ops.
    def on_release_all(self, evicted: list[str]) -> None:
        return None

    def on_evict_request(self, name: str) -> None:
        if self.mgr and name in self.mgr._resident:
            self.mgr._evict(name)

    def report_busy(self, name: str, busy: bool) -> None:
        return None
