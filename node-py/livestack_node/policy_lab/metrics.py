"""Subgroup service metrics and paired fixed-seed confidence intervals."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Iterable

from .contracts import ContractError


@dataclass(frozen=True)
class MetricRecord:
    request_id: str
    workload_class: str
    requester_region: str
    principal_id: str
    application: str
    offered: bool
    slo_good: bool
    first_output_us: int | None


@dataclass(frozen=True)
class CellMetrics:
    offered: int
    slo_good: int
    misses: int
    first_output_samples: tuple[int, ...]


def aggregate_cells(
    rows: Iterable[MetricRecord],
) -> dict[tuple[str, str, str, str], CellMetrics]:
    grouped: dict[tuple[str, str, str, str], list[MetricRecord]] = {}
    for row in rows:
        key = (row.workload_class, row.requester_region, row.principal_id, row.application)
        grouped.setdefault(key, []).append(row)
    result = {}
    for key, records in grouped.items():
        offered = [row for row in records if row.offered]
        result[key] = CellMetrics(
            offered=len(offered),
            slo_good=sum(row.slo_good for row in offered),
            misses=sum(not row.slo_good for row in offered),
            first_output_samples=tuple(
                row.first_output_us
                for row in offered
                if row.first_output_us is not None
            ),
        )
    return result


@dataclass(frozen=True)
class ConfidenceInterval:
    estimate: float
    lower: float
    upper: float
    sample_size: int
    status: str


def _index(seed: int, resample: int, draw: int, count: int) -> int:
    digest = hashlib.sha256(f"{seed}:{resample}:{draw}".encode("ascii")).digest()
    return int.from_bytes(digest[:8], "big") % count


def _quantile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(probability * len(ordered)) - 1))
    return ordered[index]


def paired_mean_ci(
    pairs: list[tuple[float, float]],
    *,
    resamples: int = 1000,
    seed: int = 0,
    minimum_pairs: int = 20,
) -> ConfidenceInterval:
    if resamples < 1000:
        raise ContractError("paired bootstrap requires at least 1000 resamples")
    if not pairs:
        return ConfidenceInterval(0, 0, 0, 0, "insufficient_evidence")
    differences = [candidate - incumbent for incumbent, candidate in pairs]
    estimate = sum(differences) / len(differences)
    if len(pairs) < minimum_pairs:
        return ConfidenceInterval(estimate, float("-inf"), float("inf"), len(pairs), "insufficient_evidence")
    bootstrapped = []
    for resample in range(resamples):
        sample = [differences[_index(seed, resample, draw, len(differences))] for draw in range(len(differences))]
        bootstrapped.append(sum(sample) / len(sample))
    return ConfidenceInterval(
        estimate,
        _quantile(bootstrapped, 0.025),
        _quantile(bootstrapped, 0.975),
        len(pairs),
        "sufficient",
    )
