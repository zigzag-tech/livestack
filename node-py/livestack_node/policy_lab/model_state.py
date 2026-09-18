"""Model residency, preparation coalescing, failure and idle eviction."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping

from .contracts import ContractError
from .resources import ReservationLedger


class ModelStateError(ContractError):
    pass


def _combine(left: Mapping[str, int], right: Mapping[str, int]) -> dict[str, int]:
    return {name: left.get(name, 0) + right.get(name, 0) for name in set(left) | set(right)}


@dataclass
class Replica:
    replica_id: str
    physical_id: str
    weights: dict[str, int]
    staging: dict[str, int]
    loadable: bool
    available: bool = True
    state: str = "absent"
    waiting_requests: set[str] = field(default_factory=set)
    active_requests: int = 0
    load_lease_id: str | None = None
    weight_lease_id: str | None = None
    residual_lease_id: str | None = None


class ReplicaManager:
    def __init__(self, ledger: ReservationLedger) -> None:
        self.ledger = ledger
        self._replicas: dict[str, Replica] = {}
        self.load_count = 0
        self.eviction_count = 0

    def define_replica(
        self,
        replica_id: str,
        physical_id: str,
        *,
        weights: Mapping[str, int],
        staging: Mapping[str, int],
        loadable: bool,
    ) -> None:
        if replica_id in self._replicas:
            raise ModelStateError(f"duplicate replica: {replica_id}")
        self._replicas[replica_id] = Replica(
            replica_id,
            self.ledger.catalog.resolve(physical_id),
            dict(weights),
            dict(staging),
            bool(loadable),
        )

    def replica(self, replica_id: str) -> Replica:
        return self._replicas[replica_id]

    def request_prepare(
        self, replica_id: str, request_id: str, *, epoch: int, fence: int
    ) -> str:
        replica = self.replica(replica_id)
        if replica.state == "resident":
            return "resident"
        if replica.state == "loading":
            replica.waiting_requests.add(request_id)
            return "coalesced"
        if replica.state != "absent":
            raise ModelStateError(f"cannot prepare replica in state {replica.state}")
        if not replica.available or not replica.loadable:
            raise ModelStateError("replica is unavailable or weights are not loadable")
        peak = _combine(replica.weights, replica.staging)
        replica.load_lease_id = self.ledger.acquire(
            f"prepare:{replica_id}", replica.physical_id, peak, epoch=epoch, fence=fence
        )
        replica.state = "loading"
        replica.waiting_requests.add(request_id)
        self.load_count += 1
        return "started"

    def finish_prepare(
        self,
        replica_id: str,
        *,
        success: bool,
        epoch: int,
        fence: int,
        residual: Mapping[str, int] | None = None,
    ) -> None:
        replica = self.replica(replica_id)
        if replica.state != "loading" or replica.load_lease_id is None:
            raise ModelStateError("replica is not loading")
        self.ledger.release_after_cleanup(replica.load_lease_id, epoch=epoch, fence=fence)
        replica.load_lease_id = None
        replica.waiting_requests.clear()
        if success:
            replica.weight_lease_id = self.ledger.acquire(
                f"weights:{replica_id}",
                replica.physical_id,
                replica.weights,
                epoch=epoch,
                fence=fence,
            )
            replica.state = "resident"
        else:
            replica.state = "absent"
            if residual and any(residual.values()):
                replica.residual_lease_id = self.ledger.acquire(
                    f"residual:{replica_id}",
                    replica.physical_id,
                    residual,
                    epoch=epoch,
                    fence=fence,
                )

    def can_serve(self, replica_id: str) -> bool:
        replica = self.replica(replica_id)
        return replica.state == "resident" and replica.available

    def start_request(self, replica_id: str) -> None:
        if not self.can_serve(replica_id):
            raise ModelStateError("execution requires a resident available replica")
        self.replica(replica_id).active_requests += 1

    def finish_request(self, replica_id: str) -> None:
        replica = self.replica(replica_id)
        if replica.active_requests <= 0:
            raise ModelStateError("replica has no active request")
        replica.active_requests -= 1

    def evict_idle(self, replica_id: str, *, epoch: int, fence: int) -> None:
        replica = self.replica(replica_id)
        if replica.state != "resident" or replica.weight_lease_id is None:
            raise ModelStateError("only resident replicas can be evicted")
        if replica.active_requests:
            raise ModelStateError("cannot evict replica with active requests")
        replica.state = "evicting"
        self.ledger.release_after_cleanup(replica.weight_lease_id, epoch=epoch, fence=fence)
        replica.weight_lease_id = None
        replica.state = "absent"
        self.eviction_count += 1
