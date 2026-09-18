"""Simulator-only bounded regional delegation and grant fencing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .contracts import ContractError


class DelegationError(ContractError):
    pass


@dataclass
class Delegation:
    region_id: str
    resource_id: str
    owner: str
    epoch: int
    limit: int
    expires_at_us: int


@dataclass
class RegionalGrant:
    grant_id: str
    region_id: str
    resource_id: str
    units: int
    epoch: int
    state: str = "active"


class DelegationManager:
    def __init__(self, physical_capacity: Mapping[str, int]) -> None:
        if not physical_capacity or any(value <= 0 for value in physical_capacity.values()):
            raise DelegationError("physical capacities must be positive")
        self.physical_capacity = dict(physical_capacity)
        self._delegations: dict[str, Delegation] = {}
        self._grants: dict[str, RegionalGrant] = {}

    def issue(
        self,
        region_id: str,
        *,
        resource_id: str,
        owner: str,
        epoch: int,
        limit: int,
        expires_at_us: int,
    ) -> None:
        if resource_id not in self.physical_capacity or min(epoch, limit, expires_at_us) < 0 or limit == 0:
            raise DelegationError("invalid delegation")
        prior = self._delegations.get(region_id)
        if prior is not None and epoch <= prior.epoch:
            raise DelegationError("delegation epoch must increase")
        other_limits = sum(
            delegation.limit
            for other_region, delegation in self._delegations.items()
            if other_region != region_id and delegation.resource_id == resource_id
        )
        active_old = sum(
            grant.units
            for grant in self._grants.values()
            if grant.state == "active" and grant.region_id == region_id and grant.resource_id == resource_id
        )
        if other_limits + max(limit, active_old) > self.physical_capacity[resource_id]:
            raise DelegationError("delegated limits exceed physical capacity")
        self._delegations[region_id] = Delegation(
            region_id, resource_id, owner, epoch, limit, expires_at_us
        )

    def grant(self, region_id: str, grant_id: str, *, units: int, epoch: int, at_us: int) -> None:
        if grant_id in self._grants or units <= 0:
            raise DelegationError("duplicate or invalid regional grant")
        delegation = self._delegations[region_id]
        if epoch != delegation.epoch:
            raise DelegationError("stale epoch for regional grant")
        if at_us >= delegation.expires_at_us:
            raise DelegationError("delegation expired")
        used = sum(
            grant.units
            for grant in self._grants.values()
            if grant.state == "active" and grant.region_id == region_id and grant.epoch == epoch
        )
        if used + units > delegation.limit:
            raise DelegationError("delegated capacity exhausted")
        if self.total_active(delegation.resource_id) + units > self.physical_capacity[delegation.resource_id]:
            raise DelegationError("physical capacity exhausted")
        self._grants[grant_id] = RegionalGrant(
            grant_id, region_id, delegation.resource_id, units, epoch
        )

    def release(self, grant_id: str, *, epoch: int) -> None:
        grant = self._grants[grant_id]
        if grant.epoch != epoch or grant.state != "active":
            raise DelegationError("stale or duplicate grant release")
        grant.state = "released"

    def total_active(self, resource_id: str) -> int:
        return sum(
            grant.units
            for grant in self._grants.values()
            if grant.resource_id == resource_id and grant.state == "active"
        )

    def grant_state(self, grant_id: str) -> str:
        return self._grants[grant_id].state

    @staticmethod
    def choose_permitted(
        candidates: Iterable[Mapping[str, Any]], *, permitted_regions: set[str]
    ) -> Mapping[str, Any]:
        eligible = [candidate for candidate in candidates if candidate.get("region") in permitted_regions]
        if not eligible:
            raise DelegationError("no permitted region candidate")
        return min(eligible, key=lambda candidate: (candidate["latency_us"], candidate["region"]))
