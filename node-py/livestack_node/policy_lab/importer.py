"""Bounded offline conversion from observation events to logical requests."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .contracts import ContractError
from .observation import ObservationEvent, ObservationJournal


TERMINAL_OUTCOMES = frozenset(
    {
        "completed",
        "refused",
        "expired",
        "client_cancel",
        "system_cancel",
        "failed",
    }
)


@dataclass(frozen=True)
class ImportedRequest:
    request_id: str
    outcome: str
    attempt_ids: tuple[str, ...]
    sampling_probability: float | None
    last_observed_at_utc_us: int
    censored: bool


@dataclass(frozen=True)
class ImportReport:
    offered_logical_requests: int
    completed: int
    refused: int
    expired: int
    client_canceled: int
    system_canceled: int
    failed: int
    unknown_outcomes: int
    attempts: int
    sampling_probabilities: tuple[float, ...]
    dropped_observation_events: int
    complete_coverage: bool


@dataclass(frozen=True)
class ImportedDataset:
    requests: tuple[ImportedRequest, ...]
    report: ImportReport


def _sampling_probability(event: ObservationEvent) -> float | None:
    value = event.payload.get("sampling_probability")
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value <= 1:
        raise ContractError("sampling_probability must be in (0, 1]")
    return float(value)


def _outcome(events: tuple[ObservationEvent, ...]) -> str:
    outcome: str | None = None
    for event in events:
        candidate: Any = None
        if event.event_type == "admission_refused":
            candidate = "refused"
        elif event.event_type == "request_terminal":
            candidate = event.payload.get("outcome")
        if candidate is None:
            continue
        if candidate not in TERMINAL_OUTCOMES:
            raise ContractError(f"invalid terminal outcome: {candidate!r}")
        if outcome is not None and outcome != candidate:
            raise ContractError(f"conflicting terminal outcomes: {outcome}, {candidate}")
        outcome = candidate
    return outcome or "unknown"


def import_observations(
    events: Iterable[Mapping[str, Any]],
    *,
    observation_horizon_utc_us: int,
    gap_summaries: Iterable[Mapping[str, Any]] = (),
) -> ImportedDataset:
    if type(observation_horizon_utc_us) is not int or observation_horizon_utc_us < 0:
        raise ContractError("observation_horizon_utc_us must be nonnegative")
    journal = ObservationJournal()
    for event in events:
        journal.append(event)

    imported: list[ImportedRequest] = []
    for request_id, joined in journal.joined_requests().items():
        arrivals = [event for event in joined.events if event.event_type == "request_arrived"]
        if not arrivals:
            continue
        sampling_values = {
            value for value in (_sampling_probability(event) for event in arrivals) if value is not None
        }
        if len(sampling_values) > 1:
            raise ContractError(f"conflicting sampling probability for {request_id}")
        outcome = _outcome(joined.events)
        last_observed = max(event.observed_at_utc_us for event in joined.events)
        imported.append(
            ImportedRequest(
                request_id=request_id,
                outcome=outcome,
                attempt_ids=joined.attempts,
                sampling_probability=next(iter(sampling_values), None),
                last_observed_at_utc_us=last_observed,
                censored=outcome == "unknown" and last_observed <= observation_horizon_utc_us,
            )
        )

    dropped = 0
    for gap in gap_summaries:
        value = gap.get("dropped_count")
        if type(value) is not int or value <= 0:
            raise ContractError("gap dropped_count must be positive")
        dropped += value
    rows = tuple(sorted(imported, key=lambda row: row.request_id))
    outcomes = [row.outcome for row in rows]
    probabilities = tuple(
        sorted({row.sampling_probability for row in rows if row.sampling_probability is not None})
    )
    report = ImportReport(
        offered_logical_requests=len(rows),
        completed=outcomes.count("completed"),
        refused=outcomes.count("refused"),
        expired=outcomes.count("expired"),
        client_canceled=outcomes.count("client_cancel"),
        system_canceled=outcomes.count("system_cancel"),
        failed=outcomes.count("failed"),
        unknown_outcomes=outcomes.count("unknown"),
        attempts=sum(len(row.attempt_ids) for row in rows),
        sampling_probabilities=probabilities,
        dropped_observation_events=dropped,
        complete_coverage=dropped == 0 and outcomes.count("unknown") == 0,
    )
    return ImportedDataset(rows, report)
