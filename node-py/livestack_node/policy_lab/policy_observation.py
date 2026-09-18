"""Delayed immutable policy observations, separate from simulator truth."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import ContractError


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in sorted(value.items())})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise ContractError(f"unsupported observation value: {type(value).__name__}")


@dataclass(frozen=True)
class TruthRequest:
    request_id: str
    arrival_us: int
    estimated_output_units: int
    actual_output_units: int


@dataclass(frozen=True)
class VisibleRequest:
    request_id: str
    arrival_us: int
    estimated_output_units: int
    observation_age_us: int


@dataclass(frozen=True)
class VisibleWorker:
    worker_id: str
    observed_at_us: int
    observation_age_us: int
    state: Mapping[str, Any]


@dataclass(frozen=True)
class PolicyObservation:
    cutoff_us: int
    requests: tuple[VisibleRequest, ...]
    workers: tuple[VisibleWorker, ...]


class ObservationBus:
    """Maintains private truth and exposes only facts delivered by cutoff."""

    def __init__(self, *, default_lag_us: int) -> None:
        if type(default_lag_us) is not int or default_lag_us < 0:
            raise ContractError("default_lag_us must be nonnegative")
        self.default_lag_us = default_lag_us
        self._requests: dict[str, TruthRequest] = {}
        self._worker_publications: list[tuple[int, int, str, Mapping[str, Any]]] = []
        self._hidden_workers: dict[str, Mapping[str, Any]] = {}

    def add_truth_request(self, request: TruthRequest) -> None:
        if request.request_id in self._requests:
            raise ContractError(f"duplicate truth request: {request.request_id}")
        if min(request.arrival_us, request.estimated_output_units, request.actual_output_units) < 0:
            raise ContractError("truth request values must be nonnegative")
        self._requests[request.request_id] = request

    def publish_worker(
        self,
        *,
        observed_at_us: int,
        worker_id: str,
        state: Mapping[str, Any],
        lag_us: int | None = None,
    ) -> None:
        lag = self.default_lag_us if lag_us is None else lag_us
        if type(observed_at_us) is not int or observed_at_us < 0 or type(lag) is not int or lag < 0:
            raise ContractError("worker observation times must be nonnegative")
        frozen = _freeze(state)
        self._worker_publications.append((observed_at_us + lag, observed_at_us, worker_id, frozen))

    def set_hidden_worker_state(self, worker_id: str, state: Mapping[str, Any]) -> None:
        self._hidden_workers[worker_id] = dict(state)

    def observe(self, cutoff_us: int) -> PolicyObservation:
        if type(cutoff_us) is not int or cutoff_us < 0:
            raise ContractError("observation cutoff must be nonnegative")
        visible_requests = tuple(
            VisibleRequest(
                request.request_id,
                request.arrival_us,
                request.estimated_output_units,
                cutoff_us - request.arrival_us,
            )
            for request in sorted(self._requests.values(), key=lambda item: item.request_id)
            if request.arrival_us + self.default_lag_us <= cutoff_us
        )
        latest: dict[str, tuple[int, Mapping[str, Any]]] = {}
        for delivered_at, observed_at, worker_id, state in sorted(self._worker_publications):
            if delivered_at <= cutoff_us:
                current = latest.get(worker_id)
                if current is None or observed_at >= current[0]:
                    latest[worker_id] = (observed_at, state)
        workers = tuple(
            VisibleWorker(worker_id, observed_at, cutoff_us - observed_at, state)
            for worker_id, (observed_at, state) in sorted(latest.items())
        )
        return PolicyObservation(cutoff_us, visible_requests, workers)
