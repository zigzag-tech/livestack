"""Clock-uncertainty and dependency-aware arrival helpers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .contracts import ContractError


class CausalityError(ContractError):
    pass


@dataclass(frozen=True)
class TimingInterval:
    status: str
    lower_us: int | None
    upper_us: int | None
    source_clock: str
    calibration_eligible: bool


def _clock_int(event: Mapping[str, Any], field: str) -> int:
    value = event.get(field)
    if type(value) is not int or value < 0:
        raise CausalityError(f"{field} must be a nonnegative integer")
    return value


def derive_interval(start: Mapping[str, Any], end: Mapping[str, Any]) -> TimingInterval:
    """Return an honest duration interval without clamping invalid samples."""

    same_boot = (
        start.get("emitter_id") == end.get("emitter_id")
        and start.get("emitter_boot_id") == end.get("emitter_boot_id")
        and start.get("emitter_id") is not None
        and start.get("emitter_boot_id") is not None
    )
    if same_boot:
        delta = _clock_int(end, "monotonic_time_us") - _clock_int(
            start, "monotonic_time_us"
        )
        if delta < 0:
            return TimingInterval("invalid", None, None, "monotonic", False)
        return TimingInterval("valid", delta, delta, "monotonic", True)

    delta = _clock_int(end, "wall_time_utc_us") - _clock_int(start, "wall_time_utc_us")
    uncertainty = _clock_int(start, "clock_uncertainty_us") + _clock_int(
        end, "clock_uncertainty_us"
    )
    lower = delta - uncertainty
    upper = delta + uncertainty
    if upper < 0:
        return TimingInterval("invalid", None, None, "utc_uncertainty", False)
    if lower < 0:
        return TimingInterval("uncertain", 0, upper, "utc_uncertainty", False)
    return TimingInterval("valid", lower, upper, "utc_uncertainty", True)


def validate_dependency_identity(requests: Mapping[str, Mapping[str, Any]]) -> None:
    for key, request in requests.items():
        if request.get("request_id") != key:
            raise CausalityError(f"request mapping identity mismatch: {key}")
        arrival = request.get("arrival")
        if not isinstance(arrival, Mapping) or arrival.get("kind") != "after_dependencies":
            continue
        dependencies = arrival.get("dependency_request_ids")
        if not isinstance(dependencies, list) or not dependencies:
            raise CausalityError(f"request {key} has invalid dependencies")
        for dependency_id in dependencies:
            dependency = requests.get(dependency_id)
            if dependency is None:
                raise CausalityError(f"request {key} has missing dependency: {dependency_id}")
            if dependency.get("workflow_id") != request.get("workflow_id"):
                raise CausalityError(
                    f"request {key} dependency {dependency_id} crosses workflow identity"
                )


def resolve_arrival_us(
    request: Mapping[str, Any], simulated_completions_us: Mapping[str, int]
) -> int:
    arrival = request.get("arrival")
    if not isinstance(arrival, Mapping):
        raise CausalityError("arrival must be an object")
    kind = arrival.get("kind")
    if kind == "external":
        value = arrival.get("relative_time_us")
        if type(value) is not int or value < 0:
            raise CausalityError("external arrival requires nonnegative relative_time_us")
        return value
    if kind != "after_dependencies":
        raise CausalityError(f"unsupported arrival kind: {kind!r}")
    dependencies = arrival.get("dependency_request_ids")
    think_time = arrival.get("think_time_us")
    if not isinstance(dependencies, list) or not dependencies:
        raise CausalityError("after_dependencies requires dependency_request_ids")
    if type(think_time) is not int or think_time < 0:
        raise CausalityError("think_time_us must be a nonnegative integer")
    completions: list[int] = []
    for dependency_id in dependencies:
        value = simulated_completions_us.get(dependency_id)
        if value is None:
            raise CausalityError(f"missing simulated completion: {dependency_id}")
        if type(value) is not int or value < 0:
            raise CausalityError(f"invalid simulated completion: {dependency_id}")
        completions.append(value)
    return max(completions) + think_time
