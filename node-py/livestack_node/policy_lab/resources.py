"""Conserved physical resources, reservations, cleanup and fencing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .contracts import ContractError


class CapacityError(ContractError):
    pass


class FenceError(ContractError):
    pass


def _vector(value: Mapping[str, int]) -> dict[str, int]:
    if not isinstance(value, Mapping) or not value:
        raise ContractError("resource vector must be a non-empty mapping")
    result: dict[str, int] = {}
    for key, amount in value.items():
        if not isinstance(key, str) or not key or type(amount) is not int or amount < 0:
            raise ContractError("resource vectors require named nonnegative integer values")
        result[key] = amount
    return result


class PhysicalResourceCatalog:
    def __init__(self) -> None:
        self.capacities: dict[str, dict[str, int]] = {}
        self.aliases: dict[str, str] = {}

    def add(
        self, physical_id: str, capacity: Mapping[str, int], *, aliases: list[str] | None = None
    ) -> None:
        if physical_id in self.capacities or physical_id in self.aliases:
            raise ContractError(f"duplicate physical resource: {physical_id}")
        self.capacities[physical_id] = _vector(capacity)
        for alias in [physical_id, *(aliases or [])]:
            if alias in self.aliases and self.aliases[alias] != physical_id:
                raise ContractError(f"resource alias collision: {alias}")
            self.aliases[alias] = physical_id

    def resolve(self, resource_id: str) -> str:
        try:
            return self.aliases[resource_id]
        except KeyError as exc:
            raise ContractError(f"unknown physical resource or alias: {resource_id}") from exc


@dataclass
class Reservation:
    lease_id: str
    owner: str
    physical_id: str
    resources: dict[str, int]
    epoch: int
    fence: int
    timed_out: bool = False
    released: bool = False


class ReservationLedger:
    def __init__(self, catalog: PhysicalResourceCatalog) -> None:
        self.catalog = catalog
        self._leases: dict[str, Reservation] = {}
        self._sequence = 0

    def used(self, resource_id: str) -> dict[str, int]:
        physical_id = self.catalog.resolve(resource_id)
        capacity = self.catalog.capacities[physical_id]
        result = {name: 0 for name in capacity}
        for lease in self._leases.values():
            if lease.physical_id == physical_id and not lease.released:
                for name, amount in lease.resources.items():
                    result[name] += amount
        return result

    def available(self, resource_id: str) -> dict[str, int]:
        physical_id = self.catalog.resolve(resource_id)
        used = self.used(physical_id)
        return {
            name: amount - used[name]
            for name, amount in self.catalog.capacities[physical_id].items()
        }

    def acquire(
        self,
        owner: str,
        resource_id: str,
        resources: Mapping[str, int],
        *,
        epoch: int,
        fence: int,
    ) -> str:
        if not owner or type(epoch) is not int or epoch < 0 or type(fence) is not int or fence < 0:
            raise ContractError("reservation owner, epoch and fence are required")
        physical_id = self.catalog.resolve(resource_id)
        requested = _vector(resources)
        available = self.available(physical_id)
        if set(requested) - set(available):
            raise CapacityError("requested resource dimension is not declared")
        if any(requested[name] > available[name] for name in requested):
            raise CapacityError(f"reservation would overcommit {physical_id}")
        lease_id = f"lease-{self._sequence}"
        self._sequence += 1
        self._leases[lease_id] = Reservation(
            lease_id, owner, physical_id, requested, epoch, fence
        )
        return lease_id

    def note_client_timeout(self, lease_id: str) -> None:
        self._leases[lease_id].timed_out = True

    def release_after_cleanup(self, lease_id: str, *, epoch: int, fence: int) -> None:
        lease = self._leases[lease_id]
        if lease.released:
            raise FenceError(f"reservation already released: {lease_id}")
        if lease.epoch != epoch or lease.fence != fence:
            raise FenceError(f"stale cleanup fence for {lease_id}")
        lease.released = True
