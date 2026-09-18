"""Comparable routing baselines and a demand-aware experimental policy."""

from __future__ import annotations

import math
from dataclasses import dataclass

from .contracts import ContractError
from .policy_contract import ActionProposal


@dataclass(frozen=True)
class RoutingCandidate:
    worker_id: str
    model_revision: str
    route_id: str
    region_id: str
    eligible: bool
    ready: bool
    loadable: bool
    distance_us: int
    queue_us: int
    preparation_us: int
    transfer_us: int
    execution_us: int
    uncertainty_us: int
    observation_age_us: int
    demand_count: float
    demand_observed_at_us: int


@dataclass(frozen=True)
class RoutingRequest:
    request_id: str
    attempt_id: str
    now_us: int
    deadline_us: int | None
    wait_age_us: int
    fairness_bound_us: int


@dataclass(frozen=True)
class CandidateTrace:
    worker_id: str
    eligible: bool
    rejected_reason: str | None
    components: dict[str, int]
    total_upper_us: int
    observation_age_us: int


@dataclass(frozen=True)
class PolicyDecision:
    policy_id: str
    chosen_worker_id: str | None
    reason_code: str
    actions: tuple[ActionProposal, ...]
    candidates: tuple[CandidateTrace, ...]


def _trace(candidate: RoutingCandidate) -> CandidateTrace:
    components = {
        "distance_us": candidate.distance_us,
        "queue_us": candidate.queue_us,
        "preparation_us": 0 if candidate.ready else candidate.preparation_us,
        "transfer_us": candidate.transfer_us,
        "execution_us": candidate.execution_us,
        "uncertainty_us": candidate.uncertainty_us,
    }
    return CandidateTrace(
        worker_id=candidate.worker_id,
        eligible=candidate.eligible and (candidate.ready or candidate.loadable),
        rejected_reason=None if candidate.eligible else "ineligible",
        components=components,
        total_upper_us=sum(components.values()),
        observation_age_us=candidate.observation_age_us,
    )


def _decision(
    policy_id: str,
    request: RoutingRequest,
    candidates: tuple[RoutingCandidate, ...],
    chosen: RoutingCandidate | None,
    reason: str,
    extras: tuple[ActionProposal, ...] = (),
) -> PolicyDecision:
    traces = tuple(_trace(candidate) for candidate in sorted(candidates, key=lambda item: item.worker_id))
    actions: tuple[ActionProposal, ...] = extras
    if chosen is not None:
        dispatch = ActionProposal(
            kind="dispatch",
            request_id=request.request_id,
            attempt_id=request.attempt_id,
            worker_id=chosen.worker_id,
            model_revision=chosen.model_revision,
            route_id=chosen.route_id,
        )
        actions = (dispatch, *extras)
    return PolicyDecision(policy_id, chosen.worker_id if chosen else None, reason, actions, traces)


def _eligible(candidates: tuple[RoutingCandidate, ...]) -> list[RoutingCandidate]:
    return [candidate for candidate in candidates if candidate.eligible and (candidate.ready or candidate.loadable)]


def nearest_ready(request: RoutingRequest, candidates: tuple[RoutingCandidate, ...]) -> PolicyDecision:
    eligible = _eligible(candidates)
    ready = [candidate for candidate in eligible if candidate.ready]
    pool = ready or eligible
    chosen = min(
        pool,
        key=lambda candidate: (
            candidate.distance_us if ready else _trace(candidate).total_upper_us,
            candidate.worker_id,
        ),
        default=None,
    )
    return _decision("nearest_ready", request, candidates, chosen, "nearest_ready" if ready else "cold_total")


def warm_first(request: RoutingRequest, candidates: tuple[RoutingCandidate, ...]) -> PolicyDecision:
    eligible = _eligible(candidates)
    ready = [candidate for candidate in eligible if candidate.ready]
    pool = ready or eligible
    if request.wait_age_us >= request.fairness_bound_us:
        chosen = min(pool, key=lambda candidate: (_trace(candidate).total_upper_us, candidate.worker_id), default=None)
        reason = "fairness_bound"
    else:
        chosen = min(pool, key=lambda candidate: (not candidate.ready, candidate.distance_us, candidate.worker_id), default=None)
        reason = "warm_first"
    return _decision("warm_first", request, candidates, chosen, reason)


def least_queue(request: RoutingRequest, candidates: tuple[RoutingCandidate, ...]) -> PolicyDecision:
    chosen = min(
        _eligible(candidates),
        key=lambda candidate: (candidate.queue_us, candidate.worker_id),
        default=None,
    )
    return _decision("least_queue", request, candidates, chosen, "least_predicted_queue")


def decayed_demand(candidate: RoutingCandidate, *, now_us: int, half_life_us: int) -> float:
    if half_life_us <= 0:
        raise ContractError("half_life_us must be positive")
    age = max(0, now_us - candidate.demand_observed_at_us)
    return candidate.demand_count * math.pow(0.5, age / half_life_us)


def residency_proposals(
    candidates: tuple[RoutingCandidate, ...], *, now_us: int, half_life_us: int, slots: int
) -> tuple[ActionProposal, ...]:
    ranked = sorted(
        (candidate for candidate in candidates if candidate.eligible),
        key=lambda candidate: (-decayed_demand(candidate, now_us=now_us, half_life_us=half_life_us), candidate.worker_id),
    )[:slots]
    return tuple(
        ActionProposal(
            kind="retain_until",
            worker_id=candidate.worker_id,
            model_revision=candidate.model_revision,
            until_us=now_us + half_life_us,
            reason_code="decayed_demand",
        )
        for candidate in ranked
    )


def total_latency_demand_aware(
    request: RoutingRequest,
    candidates: tuple[RoutingCandidate, ...],
    *,
    prepare_demand_threshold: float = math.inf,
) -> PolicyDecision:
    eligible = _eligible(candidates)
    chosen = min(
        eligible,
        key=lambda candidate: (_trace(candidate).total_upper_us, candidate.worker_id),
        default=None,
    )
    reason = "fairness_bound" if request.wait_age_us >= request.fairness_bound_us else "minimum_total_upper"
    extras = tuple(
        ActionProposal(
            kind="prepare_replica",
            worker_id=candidate.worker_id,
            model_revision=candidate.model_revision,
            route_id=candidate.route_id,
            reason_code="future_decayed_demand",
        )
        for candidate in sorted(candidates, key=lambda item: item.worker_id)
        if candidate.eligible
        and not candidate.ready
        and candidate.loadable
        and candidate.demand_count >= prepare_demand_threshold
        and (chosen is None or candidate.worker_id != chosen.worker_id)
    )
    return _decision("total_latency_demand_aware", request, candidates, chosen, reason, extras)
