"""Frozen per-cell offline promotion gates from evaluation.md."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .metrics import paired_mean_ci


@dataclass(frozen=True)
class CellComparison:
    workload_class: str
    requester_region: str
    interactive: bool
    incumbent_good: tuple[bool, ...]
    candidate_good: tuple[bool, ...]
    incumbent_first_output_us: tuple[int, ...]
    candidate_first_output_us: tuple[int, ...]
    incumbent_background_throughput: tuple[float, ...]
    candidate_background_throughput: tuple[float, ...]


@dataclass(frozen=True)
class PromotionResult:
    status: str
    reasons: tuple[str, ...]


def _ratio_pairs(incumbent: tuple[int, ...], candidate: tuple[int, ...]) -> list[tuple[float, float]]:
    return [
        (1.0, candidate_value / incumbent_value if incumbent_value else (1.0 if candidate_value <= 10_000 else math.inf))
        for incumbent_value, candidate_value in zip(incumbent, candidate)
    ]


def evaluate_promotion(
    cells: tuple[CellComparison, ...],
    *,
    claimed_primary: str,
    calibrated: bool,
    invariants_pass: bool,
    uncertainty_invariants: tuple[bool, ...],
) -> PromotionResult:
    reasons: list[str] = []
    insufficient = False
    if not calibrated:
        reasons.append("claimed domain is not calibrated")
    if not invariants_pass or not all(uncertainty_invariants):
        reasons.append("invariant failed in nominal or uncertainty sweep")
    benefit_intervals = []
    for index, cell in enumerate(cells):
        label = f"{cell.workload_class}/{cell.requester_region}"
        if len(cell.incumbent_good) != len(cell.candidate_good):
            reasons.append(f"{label}: unpaired SLO samples")
            continue
        miss_pairs = [
            (float(not incumbent), float(not candidate))
            for incumbent, candidate in zip(cell.incumbent_good, cell.candidate_good)
        ]
        miss_ci = paired_mean_ci(miss_pairs, seed=index)
        if miss_ci.status != "sufficient":
            insufficient = True
        elif cell.interactive and miss_ci.upper > 0.01:
            reasons.append(f"{label}: SLO miss regression exceeds 1 percentage point")
        if cell.interactive:
            latency_ci = paired_mean_ci(
                _ratio_pairs(cell.incumbent_first_output_us, cell.candidate_first_output_us),
                seed=100 + index,
            )
            if latency_ci.status != "sufficient":
                insufficient = True
            elif latency_ci.upper > 0.05:
                reasons.append(f"{label}: first-output ratio exceeds 1.05")
            benefit_intervals.append(latency_ci)
        if cell.incumbent_background_throughput:
            throughput_pairs = [
                (1.0, candidate / incumbent if incumbent else 1.0)
                for incumbent, candidate in zip(
                    cell.incumbent_background_throughput,
                    cell.candidate_background_throughput,
                )
            ]
            throughput_ci = paired_mean_ci(throughput_pairs, seed=200 + index)
            if throughput_ci.status != "sufficient":
                insufficient = True
            elif 1.0 + throughput_ci.lower < 0.95:
                reasons.append(f"{label}: background throughput lower bound below 0.95")
    if reasons:
        return PromotionResult("regression", tuple(reasons))
    if insufficient:
        return PromotionResult("insufficient_evidence", ("paired confidence is insufficient",))
    if claimed_primary == "first_output":
        improved = any(interval.upper <= -0.05 for interval in benefit_intervals)
        if not improved:
            return PromotionResult("no_change", ("no predeclared benefit reaches 5% with confidence",))
    return PromotionResult("offline_qualified", ())
