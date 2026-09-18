"""Strict evaluation of measured held-out traces against causal simulation output."""

from __future__ import annotations

import math
from typing import Any

from .causality import resolve_arrival_us, validate_dependency_identity
from .contracts import ContractError


FORBIDDEN_PREDICTION_FIELDS = frozenset({
    "historical_queue_wait_us",
    "recorded_queue_wait_us",
    "actual_queue_wait_us",
    "recorded_future_queue_length",
    "actual_future_queue_length",
    "historical_completion_prediction_us",
})


def _nonnegative_int(row: dict[str, Any], field: str) -> int:
    value = row.get(field)
    if type(value) is not int or value < 0:
        raise ContractError(f"{field} must be a nonnegative integer")
    return value


def evaluate_heldout_replay(dataset: Any) -> dict[str, Any]:
    """Compare measured outcomes with a replay whose arrivals remain causal."""

    if not isinstance(dataset, dict) or dataset.get("schema_version") != 1:
        raise ContractError("held-out dataset must be a schema_version 1 object")
    if dataset.get("kind") != "heldout_replay_dataset":
        raise ContractError("held-out dataset has wrong kind")
    domain_id = dataset.get("domain_id")
    if not isinstance(domain_id, str) or not domain_id:
        raise ContractError("held-out domain_id is required")
    raw_requests = dataset.get("requests")
    if not isinstance(raw_requests, list):
        raise ContractError("held-out requests must be a list")
    if not raw_requests:
        return {
            "schema_version": 1, "kind": "heldout_replay_report",
            "domain_id": domain_id, "status": "insufficient_evidence",
            "requests": [],
            "mismatch_attribution": {"completion_timing": 0, "state_transition": 0},
        }

    mapping: dict[str, dict[str, Any]] = {}
    for row in raw_requests:
        if not isinstance(row, dict):
            raise ContractError("held-out request must be an object")
        leaked = sorted(FORBIDDEN_PREDICTION_FIELDS.intersection(row))
        if leaked:
            raise ContractError(f"forbidden historical prediction field: {leaked[0]}")
        request_id = row.get("request_id")
        if not isinstance(request_id, str) or not request_id or request_id in mapping:
            raise ContractError("held-out request_id must be unique non-empty text")
        mapping[request_id] = row
    validate_dependency_identity(mapping)

    completions: dict[str, int] = {}
    reports: list[dict[str, Any]] = []
    timing_mismatches = 0
    state_mismatches = 0
    completion_errors: list[int] = []
    for row in raw_requests:
        arrival_us = resolve_arrival_us(row, completions)
        simulated_completion = _nonnegative_int(row, "simulated_completion_us")
        observed_completion = _nonnegative_int(row, "observed_completion_us")
        occupancy = _nonnegative_int(row, "observed_external_occupancy")
        if simulated_completion < arrival_us:
            raise ContractError("simulated completion precedes causal arrival")
        observed_state = row.get("observed_state")
        simulated_state = row.get("simulated_state")
        if not all(isinstance(value, str) and value for value in (observed_state, simulated_state)):
            raise ContractError("observed and simulated states are required")
        timing_mismatch = simulated_completion != observed_completion
        state_mismatch = simulated_state != observed_state
        timing_mismatches += timing_mismatch
        state_mismatches += state_mismatch
        completion_errors.append(simulated_completion - observed_completion)
        completions[row["request_id"]] = simulated_completion
        reports.append({
            "request_id": row["request_id"],
            "simulated_arrival_us": arrival_us,
            "observed_external_occupancy": occupancy,
            "observed_completion_us": observed_completion,
            "simulated_completion_us": simulated_completion,
            "completion_error_us": simulated_completion - observed_completion,
            "observed_state": observed_state,
            "simulated_state": simulated_state,
        })
    ordered_errors = sorted(completion_errors)
    quantile = lambda fraction: ordered_errors[math.ceil(len(ordered_errors) * fraction) - 1]
    return {
        "schema_version": 1, "kind": "heldout_replay_report",
        "domain_id": domain_id, "status": "evaluated", "requests": reports,
        "mismatch_attribution": {
            "completion_timing": timing_mismatches,
            "state_transition": state_mismatches,
        },
        "completion_error_distribution_us": {
            "minimum": ordered_errors[0], "p50": quantile(0.50),
            "p95": quantile(0.95), "maximum": ordered_errors[-1],
            "underpredicted": sum(value < 0 for value in completion_errors),
            "overpredicted": sum(value > 0 for value in completion_errors),
            "exact": sum(value == 0 for value in completion_errors),
        },
        "method": {
            "dependency_arrivals": "simulated_completions",
            "occupancy": "observed_external_only",
            "historical_wait_predictions": "forbidden",
        },
    }
