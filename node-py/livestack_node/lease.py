"""Lease plane (port 1) — a faithful Python port of the lease semantics in
``livestack/core/src/capabilities.ts`` (``InMemoryCapabilityLeaseStore``).

A capability is a named unit of work a node can serve (e.g. the ``asr`` or
``diarize`` model unit). A lease is a time-bounded reservation of one slot of a
capability. Leases expire after a TTL unless heartbeated; expiry is what frees a
unit for eviction (see ``residence.ResidenceController``).

Time is injectable (``clock``) and every mutating method accepts an optional
``now`` (epoch seconds), mirroring the TS ``now?: Date`` parameter, so the whole
layer is deterministic under test.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Callable, Dict, List, Mapping, Optional

Clock = Callable[[], float]


@dataclass(frozen=True)
class Capability:
    """A leasable unit a node serves. ``kind`` identifies the unit (e.g.
    ``"asr"``); at a node, one model unit == one capability ``kind``."""

    kind: str
    host_id: str
    slots: int = 1
    labels: Mapping[str, str] = field(default_factory=dict)
    lease_ttl_seconds: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("capability.kind must be non-empty")
        if not self.host_id:
            raise ValueError("capability.host_id must be non-empty")
        if self.slots < 1:
            raise ValueError("capability.slots must be >= 1")


@dataclass(frozen=True)
class Requirement:
    """What a consumer needs. Matched against registered capabilities."""

    kind: str
    min_slots: int = 1
    labels: Mapping[str, str] = field(default_factory=dict)
    host_ids: Optional[List[str]] = None


@dataclass(frozen=True)
class Lease:
    lease_id: str
    capability_kind: str
    host_id: str
    owner_id: str
    acquired_at: float
    heartbeat_at: float
    expires_at: float
    labels: Mapping[str, str] = field(default_factory=dict)


DEFAULT_TTL_SECONDS = 300


def matches_requirement(capability: Capability, requirement: Requirement) -> bool:
    """Mirror of ``matchesRequirement`` in capabilities.ts."""
    if capability.kind != requirement.kind:
        return False
    if capability.slots < requirement.min_slots:
        return False
    if requirement.host_ids is not None and capability.host_id not in requirement.host_ids:
        return False
    return all(capability.labels.get(k) == v for k, v in (requirement.labels or {}).items())


class CapabilityLeaseStore:
    """In-memory lease store. Faithful to ``InMemoryCapabilityLeaseStore``:
    register/unregister capabilities, acquire/heartbeat/release leases, and reap
    expired ones. Expiry is reaped lazily on ``acquire_lease`` (like the TS
    impl) and can also be swept explicitly via ``reap_expired``."""

    def __init__(self, clock: Optional[Clock] = None) -> None:
        self._clock: Clock = clock or time.time
        self._capabilities: Dict[str, Capability] = {}
        self._leases: Dict[str, Lease] = {}
        self._next_lease_id = 1

    # -- capabilities --------------------------------------------------------
    @staticmethod
    def _key(host_id: str, kind: str) -> str:
        return f"{host_id}::{kind}"

    def register(self, capability: Capability) -> None:
        self._capabilities[self._key(capability.host_id, capability.kind)] = capability

    def unregister(self, host_id: str, kind: Optional[str] = None) -> None:
        if kind is not None:
            self._capabilities.pop(self._key(host_id, kind), None)
            return
        for key in [k for k, c in self._capabilities.items() if c.host_id == host_id]:
            self._capabilities.pop(key, None)

    def list_capabilities(self) -> List[Capability]:
        return list(self._capabilities.values())

    def list_leases(self) -> List[Lease]:
        return list(self._leases.values())

    def active_kinds(self, now: Optional[float] = None) -> set:
        """Capability kinds that currently hold at least one unexpired lease."""
        t = self._now(now)
        return {lease.capability_kind for lease in self._leases.values() if lease.expires_at > t}

    # -- leases --------------------------------------------------------------
    def acquire_lease(
        self,
        requirement: Requirement,
        owner_id: str,
        ttl_seconds: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Optional[Lease]:
        t = self._now(now)
        self.reap_expired(t)

        candidates = sorted(
            (c for c in self._capabilities.values() if matches_requirement(c, requirement)),
            key=lambda c: (c.host_id, c.kind),
        )
        capability = next(
            (c for c in candidates if self._active_lease_count(c.host_id, c.kind) < c.slots),
            None,
        )
        if capability is None:
            return None

        ttl = ttl_seconds if ttl_seconds is not None else (
            capability.lease_ttl_seconds if capability.lease_ttl_seconds is not None else DEFAULT_TTL_SECONDS
        )
        lease = Lease(
            lease_id=f"lease-{self._next_lease_id}",
            capability_kind=capability.kind,
            host_id=capability.host_id,
            owner_id=owner_id,
            acquired_at=t,
            heartbeat_at=t,
            expires_at=t + ttl,
            labels=dict(capability.labels),
        )
        self._next_lease_id += 1
        self._leases[lease.lease_id] = lease
        return lease

    def heartbeat_lease(
        self,
        lease_id: str,
        ttl_seconds: Optional[int] = None,
        now: Optional[float] = None,
    ) -> Optional[Lease]:
        lease = self._leases.get(lease_id)
        if lease is None:
            return None
        t = self._now(now)
        capability = self._capabilities.get(self._key(lease.host_id, lease.capability_kind))
        ttl = ttl_seconds if ttl_seconds is not None else (
            capability.lease_ttl_seconds if capability and capability.lease_ttl_seconds is not None
            else DEFAULT_TTL_SECONDS
        )
        updated = replace(lease, heartbeat_at=t, expires_at=t + ttl)
        self._leases[lease_id] = updated
        return updated

    def release_lease(self, lease_id: str) -> bool:
        return self._leases.pop(lease_id, None) is not None

    def reap_expired(self, now: Optional[float] = None) -> List[Lease]:
        t = self._now(now)
        expired = [lease for lease in self._leases.values() if lease.expires_at <= t]
        for lease in expired:
            self._leases.pop(lease.lease_id, None)
        return expired

    # -- internals -----------------------------------------------------------
    def _active_lease_count(self, host_id: str, kind: str) -> int:
        return sum(
            1 for lease in self._leases.values()
            if lease.host_id == host_id and lease.capability_kind == kind
        )

    def _now(self, now: Optional[float]) -> float:
        return now if now is not None else self._clock()
