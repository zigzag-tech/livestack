"""policy_runtime.py — the fleet scheduler's target choice as a compiled policy.

``fleet_scheduler.schedule()`` places jobs; the per-job choice of WHERE is the
policy ``livestack.fleet.choose_target`` (family v1). Design:
``openspec/changes/scheduler-policy-routine/design.md`` §2–§3, and the Jingway
framework it consumes, ``jingway/openspec/changes/compiled-policy-routines/``.

This module holds the pure-Python REFERENCE of that family. The reference is a
thin wrapper over ``fleet_scheduler._feasible_candidates`` and ``_score`` — it
does not re-implement them — so it cannot drift from the code it replaced, and
the native Rust family is tested differentially against it.

Pure stdlib, like the rest of ``livestack_node``: the native module is optional.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Dict, List, Mapping, Optional, Tuple

from .fleet_scheduler import (
    DEFAULT_DISTANCE_BY_SLA, DEFAULT_SLA_SLACK_S, CostModel, Job, SchedulerPolicy,
    Sla, Target, Tier, Weights, _Fleet, _distance_n, _feasible_candidates, _fits,
    _norm, _score, _selector_matches, _utilization_n,
)

POLICY_ID = "livestack.fleet.choose_target"
FAMILY = (POLICY_ID, 1)

#: §2.3, exactly today's code. Flat names; dotted for grouping.
DEFAULT_PARAMS: Dict[str, float] = {
    "w_resource": 1.0,
    "w_budget": 1.0,
    "w_speed": 1.0,
    "w_distance": 2.0,
    "w_utilization": 1.0,
    "distance_by_sla.interactive": 1.0,
    "distance_by_sla.normal": 0.5,
    "distance_by_sla.batch": 0.1,
    "local_bonus": 1.0,
    "locality_bonus": 0.5,
    "sla_slack_s.interactive": 30.0,
    "sla_slack_s.normal": 1800.0,
    "sla_slack_s.batch": 43200.0,
}

_SLA_NAMES = {Sla.INTERACTIVE: "interactive", Sla.NORMAL: "normal", Sla.BATCH: "batch"}
_SLA_BY_NAME = {v: k for k, v in _SLA_NAMES.items()}

#: The version string decisions carry when the params came from a
#: ``SchedulerPolicy`` rather than from an artifact (``runtime=None``). Python
#: never hashes an artifact (J§3.2); this is a marker, not a version.
SCHEDULER_POLICY_VERSION = "scheduler_policy"


def params_from_policy(policy: SchedulerPolicy) -> Dict[str, float]:
    """The family params a ``SchedulerPolicy`` implies, with the same fallbacks
    ``effective_deadline`` and ``schedule()`` apply to a mapping that lacks an SLA
    (the NORMAL value)."""
    w = policy.weights
    out = {
        "w_resource": w.resource, "w_budget": w.budget, "w_speed": w.speed,
        "w_distance": w.distance, "w_utilization": w.utilization,
        "local_bonus": policy.local_bonus, "locality_bonus": policy.locality_bonus,
    }
    for sla, name in _SLA_NAMES.items():
        out[f"distance_by_sla.{name}"] = policy.distance_by_sla.get(
            sla, DEFAULT_DISTANCE_BY_SLA[Sla.NORMAL])
        out[f"sla_slack_s.{name}"] = policy.sla_slack_s.get(
            sla, DEFAULT_SLA_SLACK_S[Sla.NORMAL])
    return out


def _policy_from_params(params: Mapping[str, float]) -> SchedulerPolicy:
    return SchedulerPolicy(
        weights=Weights(resource=params["w_resource"], budget=params["w_budget"],
                        speed=params["w_speed"], distance=params["w_distance"],
                        utilization=params["w_utilization"]),
        sla_slack_s={s: params[f"sla_slack_s.{n}"] for s, n in _SLA_NAMES.items()},
        distance_by_sla={s: params[f"distance_by_sla.{n}"] for s, n in _SLA_NAMES.items()},
        local_bonus=params["local_bonus"], locality_bonus=params["locality_bonus"])


# --- §2.1 / §2.2: what one decision sees ------------------------------------
def build_ctx_and_candidates(job: Job, fleet_W: _Fleet, targets, policy: SchedulerPolicy
                             ) -> Tuple[dict, List[dict]]:
    """The family context and one candidate per target, in ``targets`` order,
    as the JSON shapes of design §2.1/§2.2.

    Everything that depends on state mutated across jobs in one ``schedule()``
    call arrives as a boolean evaluated NOW, at this job's turn: free room
    (``fits_now``), pool headroom (``headroom_ok``), instance size
    (``fits_instance``). A boolean that does not apply is ``False`` and is never
    read. ``policy`` is accepted for the signature the design names; no v1
    feature depends on it (the slacks and weights are params)."""
    ctx = {"now": fleet_W.now,
           "job": {"id": job.id, "sla": _SLA_NAMES[job.sla],
                   "created_at": job.created_at, "deadline": job.deadline,
                   "est_duration_s": job.est_duration_s,
                   "locality_host": job.locality_host}}
    cands = []
    for t in targets:
        pool = (not t.running) and t.elastic
        cands.append({"id": t.id, "features": {
            "host_id": t.host_id, "tier": t.tier.name,
            "running": t.running, "elastic": t.elastic,
            "selector_match": _selector_matches(t, job.selector),
            "fits_now": t.running and _fits(job.need, fleet_W.free.get(t.id, {})),
            "headroom_ok": pool and fleet_W.headroom(t) > 0,
            "fits_instance": pool and _fits(job.need, t.capacity),
            "provision_latency_s": t.provision_latency_s,
            "cost_per_hour": t.cost.per_hour, "cost_per_job": t.cost.per_job,
            "distance_ms": t.distance_ms, "utilization": t.utilization,
        }})
    return ctx, cands


# --- §2.4: the reference ------------------------------------------------------
# One synthetic resource and one synthetic label. The features already carry the
# answers to "does it fit" and "does the selector match"; these let
# `_feasible_candidates` ask its own questions and get exactly those answers.
_UNIT = {"_feature": 1.0}
_SEL_KEY, _SEL_VAL = "_selector_match", "1"


class _FeatureFleet(_Fleet):
    """A `_Fleet` whose free room and pool headroom are the candidates'
    booleans, so `_feasible_candidates` runs unchanged over features."""

    def __init__(self, now: float, targets, headroom_ok: Mapping[str, bool],
                 free: Mapping[str, dict]):
        # `targets` is the one attribute _feasible_candidates reads from state.
        self.state = SimpleNamespace(targets=targets)
        self.now = now
        self.free = dict(free)
        self.provisioned = {}
        self.admitted_to = set()
        self._headroom_ok = headroom_ok

    def headroom(self, t: Target) -> int:
        return 1 if self._headroom_ok.get(t.id) else 0


def _fmt(x: float) -> str:
    return f"{x:.6f}"


def choose_target_reference(params: Mapping[str, float], ctx: dict,
                            candidates: List[dict]) -> List[dict]:
    """Pure Python mirror of §2.4. Returns one row per candidate, in input order:
    ``{id, eligible, score, explorable, reason}``."""
    pol = _policy_from_params(params)
    j = ctx["job"]
    job = Job(id=j["id"], kind="", need=dict(_UNIT),
              created_at=j["created_at"], sla=_SLA_BY_NAME[j["sla"]],
              deadline=j.get("deadline"), est_duration_s=j["est_duration_s"],
              selector={_SEL_KEY: _SEL_VAL}, locality_host=j.get("locality_host"))
    targets, headroom_ok, free = [], {}, {}
    for c in candidates:
        f = c["features"]
        running = bool(f["running"])
        targets.append(Target(
            id=c["id"], host_id=f["host_id"], tier=Tier[f["tier"]],
            capacity=dict(_UNIT) if f["fits_instance"] else {},
            cost=CostModel(per_hour=f["cost_per_hour"], per_job=f["cost_per_job"]),
            provision_latency_s=f["provision_latency_s"], running=running,
            elastic=bool(f["elastic"]),
            labels={_SEL_KEY: _SEL_VAL} if f["selector_match"] else {},
            distance_ms=f["distance_ms"], utilization=f["utilization"]))
        headroom_ok[c["id"]] = bool(f["headroom_ok"])
        if running:
            free[c["id"]] = dict(_UNIT) if f["fits_now"] else {}
    fleet = _FeatureFleet(ctx["now"], tuple(targets), headroom_ok, free)
    rejected: List[Tuple[str, str]] = []
    eligible = _feasible_candidates(fleet, job, pol, rejected=rejected)
    why = dict(rejected)
    d_scale = pol.distance_by_sla[job.sla]
    w = pol.weights
    by_id = {}
    for cand in eligible:
        score = _score(cand, eligible, w, d_scale)
        cost_n = _norm(cand.est_cost, [e.est_cost for e in eligible])
        eta_n = _norm(cand.eta, [e.eta for e in eligible])
        reason = (f"scored:{_fmt(score)} cost_n={_fmt(cost_n)} eta_n={_fmt(eta_n)} "
                  f"dist_n={_fmt(_distance_n(cand, eligible))} "
                  f"util_n={_fmt(_utilization_n(cand, eligible))} "
                  f"local={_fmt(cand.local_bonus)}")
        by_id[cand.target.id] = (score, reason, cand.target)
    rows = []
    for t in targets:
        hit = by_id.get(t.id)
        if hit is None:
            rows.append({"id": t.id, "eligible": False, "score": None,
                         "explorable": False, "reason": why[t.id]})
        else:
            score, reason, _ = hit
            rows.append({"id": t.id, "eligible": True, "score": score,
                         "explorable": t.running and t.tier != Tier.LAST_RESORT,
                         "reason": reason})
    return rows


def greedy_decision(rows: List[dict], *, decision_id: str, artifact_version: str,
                    ctx: dict, candidates: List[dict]) -> dict:
    """The Jingway `Decision` (J§4.3) for a greedy policy: lowest score, ties to
    the EARLIEST input position (Python's `min` over a list), no exploration."""
    greedy = None
    best = None
    for r in rows:
        if r["eligible"] and (best is None or r["score"] < best):
            greedy, best = r["id"], r["score"]
    return {
        "decision_id": decision_id, "policy_id": POLICY_ID,
        "artifact_version": artifact_version,
        "family": {"id": FAMILY[0], "version": FAMILY[1]},
        "context": ctx, "candidates": candidates,
        "rows": rows, "greedy": greedy, "chosen": greedy, "explored": False,
        "explore_set": [greedy] if greedy is not None else [],
        "propensities": {greedy: 1.0} if greedy is not None else {},
        "exploration": {"enabled": False, "epsilon": 0.0, "margin": 0.0, "draw": None},
        "escalate": None, "shadow": [],
    }


class _ReferenceRuntime:
    """What ``schedule(runtime=None)`` decides with: the reference, the
    ``SchedulerPolicy``'s own params, no exploration, nothing recorded."""

    def __init__(self, params: Mapping[str, float]):
        self.params = dict(params)

    def decide(self, ctx: dict, candidates: List[dict], decision_id: str) -> dict:
        rows = choose_target_reference(self.params, ctx, candidates)
        return greedy_decision(rows, decision_id=decision_id,
                               artifact_version=SCHEDULER_POLICY_VERSION,
                               ctx=ctx, candidates=candidates)


_DEFAULT_POLICY = SchedulerPolicy()
_REFERENCE_RUNTIME = _ReferenceRuntime(params_from_policy(_DEFAULT_POLICY))


def reference_runtime(policy: Optional[SchedulerPolicy]) -> _ReferenceRuntime:
    """The private reference runtime for ``policy`` (shared when it is the
    default one)."""
    if policy is None or policy == _DEFAULT_POLICY:
        return _REFERENCE_RUNTIME
    return _ReferenceRuntime(params_from_policy(policy))
