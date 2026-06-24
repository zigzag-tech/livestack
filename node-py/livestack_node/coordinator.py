"""LivestackCoordinator — the lease-driven implementation of polycore.Coordinator.

polycore defines the ``Coordinator`` seam and ships a ``LocalCoordinator`` (today's
COLOAD + evict-all-on-idle). This is the livestack implementation: residence is
**lease-driven** — a unit stays resident while it holds an active lease (an explicit
broker lease from a consumer, or a usage lease refreshed on each use) or is
``HARD_PIN``. Idle eviction is per-unit; pinned units are kept (and, on an exclusive
one-model server, restored when the GPU slot frees).

Dependency direction: livestack imports polycore (for ``ResidencyPolicy``); polycore
never imports livestack. All Coordinator methods run on the GPU executor thread with
the manager's ``_guard`` held by the caller, so driving ``manager._load``/``_evict``
here is safe. ``note_usage``/broker bookkeeping touch no device state.
"""
from __future__ import annotations

import time
from typing import Callable, Dict, Optional

from .lease import Capability, CapabilityLeaseStore, Requirement

try:
    from polycore import ResidencyPolicy
    _HARD_PIN = ResidencyPolicy.HARD_PIN
except Exception:  # pragma: no cover - polycore always present where this is used
    _HARD_PIN = 0

# Usage leases with a non-positive TTL never expire (mirrors idle_seconds<=0).
_FOREVER = 100 * 365 * 24 * 3600.0


class LivestackCoordinator:
    """Implements ``polycore.Coordinator``.

    coload=True  : acquiring a unit leaves co-resident units alone (polyasr).
    coload=False : acquiring one evicts any other — one-in-VRAM (polytts).
    usage_ttl_seconds: a unit stays resident this long after its last use (the
        idle-evict knob). <=0 => used units never idle-evict.
    """

    def __init__(self, host_id: str, coload: bool = True, usage_ttl_seconds: int = 0,
                 broker: Optional[CapabilityLeaseStore] = None,
                 clock: Optional[Callable[[], float]] = None):
        self.host_id = host_id
        self.coload = coload
        self._usage_ttl = usage_ttl_seconds
        self._clock: Callable[[], float] = clock or time.monotonic
        # The broker tracks explicit consumer leases AND implicit usage leases;
        # in the single-node case it is in-process, but the same store federates.
        self.broker = broker or CapabilityLeaseStore(clock=self._clock)
        self.mgr = None
        self._usage_lease: Dict[str, str] = {}

    # --- polycore.Coordinator seam -----------------------------------------
    def bind(self, manager) -> None:
        self.mgr = manager
        for name, unit in manager.units.items():
            self.broker.register(Capability(
                kind=name, host_id=self.host_id, slots=64,
                lease_ttl_seconds=(self._usage_ttl or None),
            ))

    def acquire(self, name: str):
        m = self.mgr
        if not self.coload:
            for other in list(m._resident):
                if other != name:
                    m._evict(other)
        if name not in m._resident:
            m._load(name)
        self._note_usage(name)
        return m.units[name].model

    def idle_sweep(self) -> bool:
        m = self.mgr
        self.broker.reap_expired()
        active = self.broker.active_kinds()
        changed = False
        for name in list(m._resident):
            if self._pinned(name) or name in active:
                continue
            m._evict(name)
            self._drop_usage(name)
            changed = True
        # Keep pinned units warm: always under coload; on an exclusive server only
        # when the slot is free (never thrash an actively-leased other engine).
        pinned = [n for n, u in m.units.items() if self._pinned(n)]
        if self.coload:
            for n in pinned:
                if n not in m._resident:
                    m._load(n); changed = True
        elif pinned and not m._resident:
            m._load(pinned[0]); changed = True
        return changed

    def on_release_all(self, evicted: list) -> None:
        for name in evicted:
            self._drop_usage(name)

    def on_evict_request(self, name: str) -> None:
        m = self.mgr
        if name in m._resident and not self._pinned(name):
            m._evict(name)
            self._drop_usage(name)

    def report_busy(self, name: str, busy: bool) -> None:
        if busy:
            self._note_usage(name)

    # --- explicit consumer leases (driven by the REST facade / broker) ------
    def acquire_lease(self, kind: str, owner_id: str,
                      ttl_seconds: Optional[int] = None):
        return self.broker.acquire_lease(Requirement(kind=kind), owner_id=owner_id,
                                         ttl_seconds=ttl_seconds)

    def heartbeat_lease(self, lease_id: str, ttl_seconds: Optional[int] = None):
        return self.broker.heartbeat_lease(lease_id, ttl_seconds=ttl_seconds)

    def release_lease(self, lease_id: str) -> bool:
        return self.broker.release_lease(lease_id)

    def status(self) -> dict:
        now = self._clock()
        self.broker.reap_expired(now)
        return {
            "host_id": self.host_id,
            "coload": self.coload,
            "usage_ttl_seconds": self._usage_ttl,
            "resident": sorted(self.mgr._resident) if self.mgr else [],
            "pinned": sorted(n for n in (self.mgr.units if self.mgr else [])
                             if self._pinned(n)),
            "active_leases": [
                {"lease_id": l.lease_id, "kind": l.capability_kind, "owner_id": l.owner_id}
                for l in self.broker.list_leases()
            ],
        }

    # --- internals ----------------------------------------------------------
    def _pinned(self, name: str) -> bool:
        unit = self.mgr.units.get(name)
        return unit is not None and unit.residency_policy == _HARD_PIN

    def _note_usage(self, name: str) -> None:
        ttl = self._usage_ttl if self._usage_ttl > 0 else _FOREVER
        existing = self._usage_lease.get(name)
        if existing is not None and self.broker.heartbeat_lease(existing, ttl_seconds=ttl):
            return
        lease = self.broker.acquire_lease(Requirement(kind=name),
                                          owner_id=f"__usage__:{name}", ttl_seconds=ttl)
        if lease is not None:
            self._usage_lease[name] = lease.lease_id

    def _drop_usage(self, name: str) -> None:
        lid = self._usage_lease.pop(name, None)
        if lid is not None:
            self.broker.release_lease(lid)
