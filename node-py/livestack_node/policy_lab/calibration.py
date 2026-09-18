"""Frozen component-calibration gates for named measured domains."""

from __future__ import annotations

import math
import random
import re
from typing import Any

from .contracts import ContractError


BOOTSTRAP_RESAMPLES = 1_000
BOOTSTRAP_SEED = 1729


def _quantile(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    if not ordered:
        raise ContractError("calibration cell has no samples")
    return ordered[math.ceil(len(ordered) * fraction) - 1]


def _relative_or_absolute_error(predicted: int, observed: int, fraction: float, absolute_us: int) -> bool:
    return abs(predicted - observed) <= max(observed * fraction, absolute_us)


def _hash(value: Any, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ContractError(f"{field} must be a SHA-256 digest")
    return value


def _bootstrap_upper_error(
    samples: list[dict[str, Any]],
    *,
    observed_quantile: float,
    predicted_field: str,
    seed: int,
) -> float:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for sample in samples:
        grouped.setdefault(sample["window_id"], []).append(sample)
    keys = sorted(grouped)
    rng = random.Random(seed)
    errors: list[float] = []
    for _ in range(BOOTSTRAP_RESAMPLES):
        resampled: list[dict[str, Any]] = []
        for _ in keys:
            resampled.extend(grouped[rng.choice(keys)])
        observed = _quantile([row["observed_us"] for row in resampled], observed_quantile)
        predicted = _quantile([row[predicted_field] for row in resampled], 0.5)
        errors.append(float(abs(predicted - observed)))
    return sorted(errors)[math.ceil(len(errors) * 0.95) - 1]


def _cell_report(raw: Any, index: int) -> tuple[dict[str, Any], bool, bool]:
    if not isinstance(raw, dict):
        raise ContractError("calibration cell must be an object")
    required_text = ("cell_id", "workload_class", "requester_region", "component")
    if any(not isinstance(raw.get(field), str) or not raw[field] for field in required_text):
        raise ContractError("calibration cell identity is incomplete")
    budget = raw.get("applicable_budget_us")
    if type(budget) is not int or budget <= 0:
        raise ContractError("applicable_budget_us must be positive")
    samples = raw.get("samples")
    if not isinstance(samples, list):
        raise ContractError("calibration samples must be a list")
    numeric = (
        "observed_us", "predicted_p50_us", "predicted_p95_us",
        "interval_lower_us", "interval_upper_us",
    )
    for sample in samples:
        if not isinstance(sample, dict):
            raise ContractError("calibration sample must be an object")
        if any(type(sample.get(field)) is not int or sample[field] < 0 for field in numeric):
            raise ContractError("calibration timing fields must be nonnegative integers")
        if sample["interval_lower_us"] > sample["interval_upper_us"]:
            raise ContractError("calibration interval is reversed")
        if not isinstance(sample.get("session_id"), str) or not isinstance(sample.get("window_id"), str):
            raise ContractError("calibration sample session/window identity is required")

    required_samples = 20 if raw["component"] == "cold_preparation" else 100
    sample_ok = len(samples) >= required_samples
    windows = {sample["window_id"] for sample in samples}
    windows_ok = len(windows) >= 3
    if not samples:
        checks = {
            "sample_count": False, "observation_windows": False, "p50_error": False,
            "p95_error": False, "interval_coverage": False, "interval_width": False,
            "memory_underprediction": False,
        }
        return {"cell_id": raw["cell_id"], "checks": checks, "sample_count": 0}, True, False

    observed = [sample["observed_us"] for sample in samples]
    observed_p50 = _quantile(observed, 0.50)
    observed_p95 = _quantile(observed, 0.95)
    predicted_p50 = _quantile([sample["predicted_p50_us"] for sample in samples], 0.50)
    predicted_p95 = _quantile([sample["predicted_p95_us"] for sample in samples], 0.50)
    p50_upper_error = _bootstrap_upper_error(
        samples, observed_quantile=0.50, predicted_field="predicted_p50_us", seed=BOOTSTRAP_SEED + index * 2
    ) if windows else math.inf
    p95_upper_error = _bootstrap_upper_error(
        samples, observed_quantile=0.95, predicted_field="predicted_p95_us", seed=BOOTSTRAP_SEED + index * 2 + 1
    ) if windows else math.inf
    p50_limit = max(observed_p50 * 0.20, 50_000)
    p95_limit = max(observed_p95 * 0.25, 100_000)
    coverage = sum(
        sample["interval_lower_us"] <= sample["observed_us"] <= sample["interval_upper_us"]
        for sample in samples
    ) / len(samples)
    interval_width = max(sample["interval_upper_us"] - sample["interval_lower_us"] for sample in samples)
    memory_rows = [
        sample for sample in samples
        if "observed_peak_memory_bytes" in sample or "predicted_peak_memory_bytes" in sample
    ]
    memory_ok = bool(memory_rows) and all(
        type(sample.get("observed_peak_memory_bytes")) is int
        and type(sample.get("predicted_peak_memory_bytes")) is int
        and sample["predicted_peak_memory_bytes"] + max(
            sample["observed_peak_memory_bytes"] * 0.05, 128 * 1024**2
        ) >= sample["observed_peak_memory_bytes"]
        for sample in memory_rows
    )
    checks = {
        "sample_count": sample_ok,
        "observation_windows": windows_ok,
        "p50_error": _relative_or_absolute_error(predicted_p50, observed_p50, 0.20, 50_000)
        and p50_upper_error <= p50_limit,
        "p95_error": _relative_or_absolute_error(predicted_p95, observed_p95, 0.25, 100_000)
        and p95_upper_error <= p95_limit,
        "interval_coverage": 0.85 <= coverage <= 0.95,
        "interval_width": interval_width <= budget,
        "memory_underprediction": memory_ok,
    }
    insufficient = not sample_ok or not windows_ok
    failed = any(not value for name, value in checks.items() if name not in {"sample_count", "observation_windows"})
    return {
        "cell_id": raw["cell_id"],
        "workload_class": raw["workload_class"],
        "requester_region": raw["requester_region"],
        "component": raw["component"],
        "sample_count": len(samples),
        "observation_windows": len(windows),
        "observed_p50_us": observed_p50,
        "observed_p95_us": observed_p95,
        "predicted_p50_us": predicted_p50,
        "predicted_p95_us": predicted_p95,
        "p50_bootstrap_95pct_upper_error_us": p50_upper_error,
        "p95_bootstrap_95pct_upper_error_us": p95_upper_error,
        "prediction_interval_coverage": coverage,
        "maximum_interval_width_us": interval_width,
        "checks": checks,
    }, insufficient, failed


def evaluate_calibration(dataset: Any) -> dict[str, Any]:
    if not isinstance(dataset, dict) or dataset.get("schema_version") != 1:
        raise ContractError("calibration dataset must be a schema_version 1 object")
    if dataset.get("kind") != "calibration_dataset":
        raise ContractError("calibration dataset has wrong kind")
    domain_id = dataset.get("domain_id")
    if not isinstance(domain_id, str) or not domain_id:
        raise ContractError("calibration domain_id is required")
    digests = {name: _hash(dataset.get(name), name) for name in (
        "profile_sha256", "dataset_sha256", "evaluator_sha256"
    )}
    raw_cells = dataset.get("cells")
    if not isinstance(raw_cells, list):
        raise ContractError("calibration cells must be a list")
    reports: list[dict[str, Any]] = []
    insufficient = not raw_cells
    failed = False
    for index, raw in enumerate(raw_cells):
        report, cell_insufficient, cell_failed = _cell_report(raw, index)
        reports.append(report)
        insufficient = insufficient or cell_insufficient
        failed = failed or cell_failed
    status = "failed" if failed else ("insufficient_evidence" if insufficient else "calibrated")
    certificate = None
    if status == "calibrated":
        certificate = {
            "domain_id": domain_id,
            "profile_sha256": digests["profile_sha256"],
            "dataset_sha256": digests["dataset_sha256"],
            "evaluator_sha256": digests["evaluator_sha256"],
        }
    return {
        "schema_version": 1,
        "kind": "calibration_report",
        "domain_id": domain_id,
        "status": status,
        "cells": reports,
        "certificate": certificate,
        "unqualified_domains": [] if certificate else [domain_id],
        "method": {
            "bootstrap_unit": "observation_window",
            "bootstrap_resamples": BOOTSTRAP_RESAMPLES,
            "bootstrap_seed": BOOTSTRAP_SEED,
        },
    }
