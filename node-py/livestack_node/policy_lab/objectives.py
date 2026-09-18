"""Strict workload objectives and honest offered-load denominators."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .contracts import ContractError, StrictObjectSpec


OBJECTIVE_SPEC = StrictObjectSpec(
    required=frozenset(
        {
            "schema_version",
            "kind",
            "objective_id",
            "priority_class",
            "first_output_budget_us",
            "completion_budget_us",
            "streaming_lag_budget_us",
            "allowable_miss_rate",
            "fairness_start_bound_us",
        }
    )
)


@dataclass(frozen=True)
class ObjectiveProfile:
    objective_id: str
    priority_class: str
    first_output_budget_us: int | None
    completion_budget_us: int | None
    streaming_lag_budget_us: int | None
    allowable_miss_rate: float
    fairness_start_bound_us: int | None


def _optional_nonnegative_int(value: Any, name: str) -> int | None:
    if value is None:
        return None
    if type(value) is not int or value < 0:
        raise ContractError(f"{name} must be null or a nonnegative integer")
    return value


def parse_objective(value: Any) -> ObjectiveProfile:
    item = OBJECTIVE_SPEC.validate(value, path="objective profile")
    if item["kind"] != "objective_profile":
        raise ContractError("objective kind must be objective_profile")
    for name in ("objective_id", "priority_class"):
        if not isinstance(item[name], str) or not item[name]:
            raise ContractError(f"{name} must be non-empty")
    miss_rate = item["allowable_miss_rate"]
    if isinstance(miss_rate, bool) or not isinstance(miss_rate, (int, float)):
        raise ContractError("allowable_miss_rate must be numeric")
    if not 0 <= miss_rate <= 1:
        raise ContractError("allowable_miss_rate must be in [0, 1]")
    return ObjectiveProfile(
        objective_id=item["objective_id"],
        priority_class=item["priority_class"],
        first_output_budget_us=_optional_nonnegative_int(
            item["first_output_budget_us"], "first_output_budget_us"
        ),
        completion_budget_us=_optional_nonnegative_int(
            item["completion_budget_us"], "completion_budget_us"
        ),
        streaming_lag_budget_us=_optional_nonnegative_int(
            item["streaming_lag_budget_us"], "streaming_lag_budget_us"
        ),
        allowable_miss_rate=float(miss_rate),
        fairness_start_bound_us=_optional_nonnegative_int(
            item["fairness_start_bound_us"], "fairness_start_bound_us"
        ),
    )


@dataclass(frozen=True)
class OfferedLoadReport:
    offered_eligible: int
    slo_good: int
    refused: int
    external_user_canceled: int
    system_canceled: int
    denominator_excluding_external_cancels: int
    denominator_treating_cancels_as_failures: int
    good_fraction_excluding_external_cancels: float
    good_fraction_all: float


def _within(value: Any, budget: int | None) -> bool:
    if budget is None:
        return True
    return type(value) is int and value >= 0 and value <= budget


def evaluate_offered_load(
    objective: ObjectiveProfile, rows: Iterable[Mapping[str, Any]]
) -> OfferedLoadReport:
    offered = 0
    good = 0
    refused = 0
    external_cancel = 0
    system_cancel = 0
    seen: set[str] = set()
    for row in rows:
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ContractError("metric row requires request_id")
        if request_id in seen:
            raise ContractError(f"duplicate logical request metric: {request_id}")
        seen.add(request_id)
        if row.get("eligible") is not True:
            continue
        offered += 1
        outcome = row.get("outcome")
        cancellation_cause = row.get("cancellation_cause")
        if outcome == "refused":
            refused += 1
        if cancellation_cause == "external_user":
            external_cancel += 1
        elif cancellation_cause is not None:
            system_cancel += 1
        if (
            outcome == "completed"
            and _within(row.get("first_output_us"), objective.first_output_budget_us)
            and _within(row.get("completion_us"), objective.completion_budget_us)
            and _within(row.get("streaming_lag_us"), objective.streaming_lag_budget_us)
        ):
            good += 1
    excluding = offered - external_cancel
    return OfferedLoadReport(
        offered_eligible=offered,
        slo_good=good,
        refused=refused,
        external_user_canceled=external_cancel,
        system_canceled=system_cancel,
        denominator_excluding_external_cancels=excluding,
        denominator_treating_cancels_as_failures=offered,
        good_fraction_excluding_external_cancels=good / excluding if excluding else 0.0,
        good_fraction_all=good / offered if offered else 0.0,
    )
