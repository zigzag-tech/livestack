"""Correlate existing Harmony facts without upgrading tentative decisions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from ..contracts import ContractError


@dataclass(frozen=True)
class HarmonyCorrelation:
    request_id: str
    tentative_grants: tuple[str, ...]
    prepare_failures: tuple[str, ...]
    served_attempts: tuple[str, ...]
    terminal_outcome: str


def correlate_harmony_records(
    records: Iterable[Mapping[str, Any]],
) -> dict[str, HarmonyCorrelation]:
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        request_id = record.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            raise ContractError("Harmony correlation record requires request_id")
        state = grouped.setdefault(
            request_id,
            {"grants": set(), "failures": [], "served": set(), "outcome": "unknown"},
        )
        kind = record.get("kind")
        if kind == "decision" and record.get("chosen") is not None:
            decision_id = record.get("decision_id")
            if not isinstance(decision_id, str) or not decision_id:
                raise ContractError("tentative decision requires decision_id")
            state["grants"].add(decision_id)
        elif kind == "prepare_failed":
            failure = record.get("failure_class")
            if not isinstance(failure, str) or not failure:
                raise ContractError("prepare failure requires failure_class")
            state["failures"].append(failure)
        elif kind in {"first_output", "execution_finished"}:
            attempt_id = record.get("attempt_id")
            if not isinstance(attempt_id, str) or not attempt_id:
                raise ContractError("serving evidence requires attempt_id")
            state["served"].add(attempt_id)
            if kind == "execution_finished":
                state["outcome"] = record.get("outcome", "unknown")
    return {
        request_id: HarmonyCorrelation(
            request_id,
            tuple(sorted(state["grants"])),
            tuple(state["failures"]),
            tuple(sorted(state["served"])),
            state["outcome"],
        )
        for request_id, state in sorted(grouped.items())
    }
