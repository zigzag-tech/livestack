"""Residence controller (port 3) — the missing bridge.

In the raw lease store, expiry only deletes a record (``reap_expired``); nothing
frees VRAM. The controller binds the lease lifecycle to a ``ModelRuntime`` and
enforces the single residence rule:

    a unit is resident iff it holds >=1 active lease, or is pinned.

This is the one mechanism that subsumes the old idle-timer, manual unload, and
pin flag. "Idle-evict after N seconds" == "no lease renewed within the TTL".
"Pinned" == a unit the controller always keeps resident.

Two concurrency rules matter for embedding in a real GPU server:

- ``reconcile`` performs warm/evict (GPU work) *without* holding the bookkeeping
  lock, so lease heartbeats never block behind a multi-GB model load. Reconciles
  serialize against each other via a separate lock; the runtime is responsible
  for serializing the actual GPU op (e.g. polyasr's ``_transcribe_lock``).
- ``note_usage`` is pure bookkeeping (no GPU work, no reconcile), so it is safe
  to call from inside the GPU critical section on every model use.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, List, Optional

from .lease import Capability, CapabilityLeaseStore, Lease, Requirement
from .runtime import ModelRuntime

Clock = Callable[[], float]

# Usage leases with a non-positive TTL never expire (mirrors the historical
# "IDLE_EVICT_SECONDS=0 == never evict"); modelled as a very long finite TTL.
_FOREVER = 100 * 365 * 24 * 3600.0


class ResidenceController:
    """Owns a lease store + a runtime, and reconciles desired vs actual
    residence. ``reconcile`` is directly callable with an injected ``now`` for
    deterministic tests (no background thread needed).

    usage_ttl_seconds: TTL for the implicit per-unit "usage" lease refreshed on
        every model use (see ``note_usage``). This is the idle-evict knob: <=0
        means a used unit stays resident forever (historical behaviour)."""

    def __init__(
        self,
        host_id: str,
        runtime: ModelRuntime,
        units: Dict[str, Capability],
        usage_ttl_seconds: int = 0,
        proactive_warm: bool = True,
        clock: Optional[Clock] = None,
    ) -> None:
        self.host_id = host_id
        self._runtime = runtime
        self._usage_ttl = usage_ttl_seconds
        # proactive_warm=True (co-load servers, e.g. polyasr): reconcile loads any
        # desired-but-absent unit. proactive_warm=False (exclusive one-model-in-VRAM
        # servers, e.g. polytts): the controller only EVICTS; loading stays lazy via
        # the server's own ensure(), so two leased engines can't thrash each other.
        self._proactive_warm = proactive_warm
        self._clock: Clock = clock or time.time
        self._store = CapabilityLeaseStore(clock=self._clock)
        self._pinned: set = set()
        self._usage_lease: Dict[str, str] = {}
        self._lock = threading.RLock()         # protects store + bookkeeping
        self._reconcile_lock = threading.Lock()  # serializes reconciles (GPU ops)
        self._sweep_stop: Optional[threading.Event] = None
        self._sweep_thread: Optional[threading.Thread] = None
        for unit, capability in units.items():
            if capability.kind != unit:
                raise ValueError(
                    f"unit '{unit}' must register a capability whose kind == unit "
                    f"(got kind='{capability.kind}')"
                )
            self._store.register(capability)

    # -- lease lifecycle (called by the REST facade / embedded consumers) ----
    def acquire(
        self,
        kind: str,
        owner_id: str,
        ttl_seconds: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Optional[Lease]:
        with self._lock:
            lease = self._store.acquire_lease(
                Requirement(kind=kind), owner_id=owner_id, ttl_seconds=ttl_seconds, now=now
            )
        if lease is not None:
            # warm-on-acquire: pay the reload before the caller's first request.
            self.reconcile(now)
        return lease

    def heartbeat(self, lease_id: str, ttl_seconds: Optional[int] = None,
                  now: Optional[float] = None) -> Optional[Lease]:
        with self._lock:
            return self._store.heartbeat_lease(lease_id, ttl_seconds=ttl_seconds, now=now)

    def release(self, lease_id: str, now: Optional[float] = None) -> bool:
        with self._lock:
            ok = self._store.release_lease(lease_id)
        self.reconcile(now)
        return ok

    def pin(self, unit: str, pinned: bool = True, now: Optional[float] = None) -> None:
        with self._lock:
            if pinned:
                self._pinned.add(unit)
            else:
                self._pinned.discard(unit)
        self.reconcile(now)

    def note_usage(self, unit: str, now: Optional[float] = None) -> None:
        """Refresh the implicit usage lease for ``unit`` — call on every model
        use. Pure bookkeeping: never loads/evicts, never reconciles, so it is
        safe inside the GPU critical section. The background sweep does the
        actual eviction once the usage lease (and any explicit lease) lapses."""
        ttl = self._usage_ttl if self._usage_ttl > 0 else _FOREVER
        with self._lock:
            existing = self._usage_lease.get(unit)
            if existing is not None and self._store.heartbeat_lease(existing, ttl_seconds=ttl, now=now):
                return
            lease = self._store.acquire_lease(
                Requirement(kind=unit), owner_id=f"__usage__:{unit}", ttl_seconds=ttl, now=now
            )
            if lease is not None:
                self._usage_lease[unit] = lease.lease_id

    # -- reconciliation ------------------------------------------------------
    def reconcile(self, now: Optional[float] = None) -> None:
        """Sweep expired leases and converge VRAM residence to the desired set.
        Runtime warm/evict run WITHOUT the bookkeeping lock held."""
        with self._reconcile_lock:
            with self._lock:
                self._store.reap_expired(now)
                pinned = set(self._pinned)
                desired = set(self._store.active_kinds(now)) | pinned
            current = set(self._runtime.resident())
            for unit in sorted(current - desired):
                self._runtime.evict(unit)
            current = set(self._runtime.resident())
            if self._proactive_warm:
                # Co-load server: load every desired-but-absent unit.
                for unit in sorted(desired - current):
                    self._runtime.warm(unit)
            elif pinned and not current:
                # Exclusive one-model server: the GPU slot is free, so restore a
                # pinned unit (e.g. polytts' benchday engine). Only when free, so
                # an actively-leased other engine is never thrashed.
                self._runtime.warm(sorted(pinned)[0])

    # -- introspection (port 2 /health) -------------------------------------
    def status(self, now: Optional[float] = None) -> dict:
        with self._lock:
            self._store.reap_expired(now)
            return {
                "host_id": self.host_id,
                "units": [c.kind for c in self._store.list_capabilities()],
                "resident": sorted(self._runtime.resident()),
                "pinned": sorted(self._pinned),
                "usage_ttl_seconds": self._usage_ttl,
                "active_leases": [
                    {
                        "lease_id": lease.lease_id,
                        "kind": lease.capability_kind,
                        "owner_id": lease.owner_id,
                        "expires_at": lease.expires_at,
                    }
                    for lease in self._store.list_leases()
                ],
            }

    # -- optional background sweep (production; not used in tests) -----------
    def start_sweep(self, interval_seconds: float = 10.0) -> None:
        if self._sweep_thread is not None:
            return
        self._sweep_stop = threading.Event()

        def _loop() -> None:
            assert self._sweep_stop is not None
            while not self._sweep_stop.wait(interval_seconds):
                self.reconcile()

        self._sweep_thread = threading.Thread(target=_loop, name="residence-sweep", daemon=True)
        self._sweep_thread.start()

    def stop_sweep(self) -> None:
        if self._sweep_stop is not None:
            self._sweep_stop.set()
        if self._sweep_thread is not None:
            self._sweep_thread.join(timeout=1.0)
        self._sweep_thread = None
        self._sweep_stop = None
