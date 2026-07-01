"""fleet_scheduler.py — resource/budget/speed-aware admission & burst for livestack.

The brain *above* ``planner.py``. Where the placement planner decides **what is
resident where** over devices that already exist, the fleet scheduler decides the
**set of targets** — run now on an existing worker, wait, spin up cheap cloud, or
(last resort) burst to expensive serverless (RunPod) — and *when to pay for more*.
Design: ``_plans/fleet-scheduler.md``.

Like the planner it is a **pure function** — ``schedule(FleetState) -> FleetPlan`` —
with no I/O and an injectable ``now``; the broker assembles the FleetState (from
peer ``/status`` + a spend ledger) and dispatches the actions (provision via the
``aliyun``/``runpod`` adapters, admit via the job client). One shared weight vector
(:class:`Weights`) parameterizes both brains, so this is one system, not a bolt-on.

Three axes, per the design decisions:

* **resource-aware** — capability/selector fit + a prefer-local/keep-warm bonus.
* **budget-aware (SOFT)** — cost is a *term in the objective* (``w_budget``) and a
  *pressure signal* (over-target spend nudges the weights toward budget-first). It
  **never blocks or rejects** work — there is no ``Reject`` action.
* **speed-aware** — every job has an effective deadline (from its SLA class, or an
  explicit override); a target is a candidate only if it can meet that deadline,
  and jobs are placed earliest-deadline-first.

RunPod is modelled as the most expensive :class:`Tier` (``LAST_RESORT``) and is only
a candidate once every cheaper feasible tier fails to meet the deadline — a hard
lexicographic guard, so "last resort" is literal, not just a high cost weight.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple, Union

from .planner import Res, _fits, _sub

_EPS = 1e-9


# --- model ------------------------------------------------------------------
class Tier(enum.IntEnum):
    """Cost-ascending target classes. Prefer-local and RunPod-last-resort fall out
    of the ordering + cost term rather than being special-cased."""
    LOCAL = 0        # tower0 GPU: sunk cost (~0), fixed capacity
    SPOT = 1         # cheap elastic cloud (e.g. Aliyun ECI / spot)
    ONDEMAND = 2     # medium elastic cloud (e.g. Aliyun g8i on-demand)
    LAST_RESORT = 3  # RunPod serverless: expensive, near-instant scale


class Sla(enum.IntEnum):
    """Speed intent. Sets a *default* deadline slack; an explicit ``deadline`` wins."""
    INTERACTIVE = 0
    NORMAL = 1
    BATCH = 2


@dataclass(frozen=True)
class CostModel:
    """Money a target costs to run one job. ``estimate`` = flat + hourly*runtime."""
    per_hour: float = 0.0
    per_job: float = 0.0

    def estimate(self, duration_s: float) -> float:
        return self.per_job + self.per_hour * (max(0.0, duration_s) / 3600.0)


@dataclass(frozen=True)
class Target:
    """Somewhere a job can run: an existing worker, or an elastic pool we can spin
    up. Generalizes ``planner.Device`` with money + time + elasticity.

    A *running* target (``running=True``) exposes its **free** capacity now and is
    used via :class:`Admit`. An *elastic pool* (``running=False, elastic=True``)
    exposes per-instance ``capacity`` and is used via :class:`Provision` (bounded by
    ``max_instances``; ``running_instances`` already up feeds deprovision hysteresis).
    """
    id: str
    host_id: str
    tier: Tier
    capacity: Res                              # free now (running) / per-instance (pool)
    cost: CostModel = CostModel()
    provision_latency_s: float = 0.0           # 0 if already running
    running: bool = True
    elastic: bool = False
    max_instances: int = 1
    running_instances: int = 0                 # up right now (for hysteresis)
    up_since: float = 0.0                       # when the running instance came up
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Job:
    """A pending unit of work to place. ``need`` is a generic resource vector
    (default one slot); ``kind``/``selector`` gate which targets can run it."""
    id: str
    kind: str
    need: Res = field(default_factory=lambda: {"slot": 1.0})
    owner: str = "anon"
    created_at: float = 0.0
    sla: Sla = Sla.NORMAL
    deadline: Optional[float] = None           # explicit; else derived from sla
    est_duration_s: float = 60.0
    selector: Mapping[str, str] = field(default_factory=dict)
    locality_host: Optional[str] = None        # where the input lives (prefer here)


@dataclass(frozen=True)
class Ledger:
    """Advisory spend for the current window. Budget is SOFT: this never gates work,
    it only makes ``pressure`` for the weight resolver (over-target => budget-first)."""
    spent: float = 0.0
    target: float = 0.0                        # 0 => no target set

    def pressure(self) -> float:
        if self.target <= _EPS:
            return 0.0
        return max(0.0, (self.spent - self.target) / self.target)


@dataclass(frozen=True)
class Weights:
    """The multi-objective knob shared with the placement planner. Higher = matters
    more. ``budget`` prefers cheap, ``speed`` prefers low-ETA, ``resource`` prefers
    local/keep-warm."""
    resource: float = 1.0
    budget: float = 1.0
    speed: float = 1.0


# Default deadline slack per SLA class (seconds) — the class picks this; an explicit
# Job.deadline overrides. INTERACTIVE is tight (will burst), BATCH is loose (waits).
DEFAULT_SLA_SLACK_S: Mapping[Sla, float] = {
    Sla.INTERACTIVE: 30.0,
    Sla.NORMAL: 30 * 60.0,
    Sla.BATCH: 12 * 3600.0,
}


@dataclass(frozen=True)
class SchedulerPolicy:
    weights: Weights = field(default_factory=Weights)
    sla_slack_s: Mapping[Sla, float] = field(default_factory=lambda: dict(DEFAULT_SLA_SLACK_S))
    local_bonus: float = 1.0            # resource-term reward for a LOCAL target
    locality_bonus: float = 0.5         # extra reward when the input already lives there
    min_uptime_s: float = 120.0         # anti-thrash: don't deprovision a burst worker this soon


# --- actions ----------------------------------------------------------------
@dataclass(frozen=True)
class Admit:
    """Run ``job`` now on an existing running ``target``."""
    job_id: str
    target_id: str
    est_cost: float
    reason: str = ""


@dataclass(frozen=True)
class Provision:
    """Spin up one instance of an elastic ``target`` pool and run ``job`` on it."""
    target_id: str
    job_id: str
    tier: Tier
    est_cost: float
    reason: str = ""


@dataclass(frozen=True)
class Queue:
    """Hold ``job`` — nothing can take it now within policy (local will free / off-peak)."""
    job_id: str
    reason: str = ""


@dataclass(frozen=True)
class Deprovision:
    """Drain an idle elastic ``target`` (hysteresis) — no demand, past min-uptime."""
    target_id: str
    reason: str = ""


FleetAction = Union[Admit, Provision, Queue, Deprovision]


@dataclass(frozen=True)
class FleetPlan:
    actions: Tuple[FleetAction, ...]

    def of(self, cls) -> List[FleetAction]:
        return [a for a in self.actions if isinstance(a, cls)]

    def summary(self) -> str:
        parts = []
        for a in self.actions:
            if isinstance(a, Admit):
                parts.append(f"admit {a.job_id}->{a.target_id} (¥{a.est_cost:.2f})")
            elif isinstance(a, Provision):
                parts.append(f"provision {a.target_id} for {a.job_id} "
                             f"[{a.tier.name}] (¥{a.est_cost:.2f})")
            elif isinstance(a, Queue):
                parts.append(f"queue {a.job_id} ({a.reason})")
            elif isinstance(a, Deprovision):
                parts.append(f"deprovision {a.target_id} ({a.reason})")
        return "; ".join(parts)


@dataclass(frozen=True)
class FleetState:
    targets: Tuple[Target, ...]
    jobs: Tuple[Job, ...] = ()
    now: float = 0.0
    ledger: Ledger = field(default_factory=Ledger)


# --- helpers ----------------------------------------------------------------
def effective_deadline(job: Job, policy: SchedulerPolicy) -> float:
    if job.deadline is not None:
        return job.deadline
    slack = policy.sla_slack_s.get(job.sla, DEFAULT_SLA_SLACK_S[Sla.NORMAL])
    return job.created_at + slack


def _selector_matches(target: Target, selector: Mapping[str, str]) -> bool:
    return all(target.labels.get(k) == v for k, v in selector.items())


def _eta(target: Target, job: Job) -> float:
    """Wall-clock until ``job`` finishes on ``target``: (provision if cold) + runtime.
    MVP does not model deep queue-wait — a target is only a candidate if it has room
    now (running) or an instance to spin (pool), so wait is 0 or the provision cost."""
    warmup = 0.0 if target.running else target.provision_latency_s
    return warmup + job.est_duration_s


def _local_bonus(target: Target, job: Job, policy: SchedulerPolicy) -> float:
    bonus = policy.local_bonus if target.tier == Tier.LOCAL else 0.0
    if job.locality_host is not None and job.locality_host == target.host_id:
        bonus += policy.locality_bonus
    return bonus


@dataclass
class _Cand:
    target: Target
    provision: bool          # True => Provision a pool instance; False => Admit on running
    est_cost: float
    eta: float
    local_bonus: float


class _Fleet:
    """Mutable working copy the greedy scheduler mutates as it commits decisions."""

    def __init__(self, state: FleetState):
        self.state = state
        self.now = state.now
        # running target_id -> free Res (consumed by Admit)
        self.free: Dict[str, Dict[str, float]] = {
            t.id: dict(t.capacity) for t in state.targets if t.running
        }
        # pool target_id -> instances provisioned this cycle (bounded by headroom)
        self.provisioned: Dict[str, int] = {t.id: 0 for t in state.targets}
        self.admitted_to: set = set()          # running targets that took a job this cycle

    def headroom(self, t: Target) -> int:
        return max(0, t.max_instances - t.running_instances - self.provisioned.get(t.id, 0))


def _feasible_candidates(fleet: _Fleet, job: Job, policy: SchedulerPolicy) -> List[_Cand]:
    """Every target that can run ``job`` AND meet its deadline. Applies the RunPod
    last-resort guard: LAST_RESORT is dropped whenever a cheaper tier is feasible."""
    dl = effective_deadline(job, policy)
    cands: List[_Cand] = []
    for t in fleet.state.targets:
        if not _selector_matches(t, job.selector):
            continue
        eta = _eta(t, job)
        if fleet.now + eta > dl + _EPS:                # cannot meet the deadline
            continue
        if t.running:
            if not _fits(job.need, fleet.free.get(t.id, {})):
                continue                                # no room right now
            provision = False
        elif t.elastic:
            if fleet.headroom(t) <= 0:                  # pool at its instance cap
                continue
            if not _fits(job.need, t.capacity):         # one instance can't hold the job
                continue
            provision = True
        else:
            continue                                    # cold, non-elastic: unusable
        cands.append(_Cand(
            target=t, provision=provision,
            est_cost=t.cost.estimate(job.est_duration_s),
            eta=eta, local_bonus=_local_bonus(t, job, policy),
        ))
    if not cands:
        return []
    # RunPod last-resort guard (lexicographic, before the weighted score): only keep
    # LAST_RESORT candidates if NO cheaper tier is feasible.
    cheaper = [c for c in cands if c.target.tier < Tier.LAST_RESORT]
    return cheaper if cheaper else cands


def _score(cand: _Cand, cands: List[_Cand], weights: Weights) -> float:
    """Lower is better. Min-max normalize cost & ETA across the candidate set so the
    weights are comparable, then: w_budget*cost + w_speed*eta - w_resource*local."""
    costs = [c.est_cost for c in cands]
    etas = [c.eta for c in cands]
    cost_n = _norm(cand.est_cost, costs)
    eta_n = _norm(cand.eta, etas)
    return (weights.budget * cost_n
            + weights.speed * eta_n
            - weights.resource * cand.local_bonus)


def _norm(v: float, xs: List[float]) -> float:
    lo, hi = min(xs), max(xs)
    if hi - lo < _EPS:
        return 0.0
    return (v - lo) / (hi - lo)


# --- the scheduler ----------------------------------------------------------
def schedule(state: FleetState, policy: Optional[SchedulerPolicy] = None) -> FleetPlan:
    """Compute the fleet admission/burst plan for ``state``. Pure function."""
    pol = policy or SchedulerPolicy()
    W = _Fleet(state)
    actions: List[FleetAction] = []

    # Place jobs earliest-effective-deadline first (EDF). As `now` advances a waiting
    # job's slack shrinks, so it naturally rises in urgency and eventually forces a
    # burst — no separate aging bookkeeping needed here (the weight resolver handles
    # the global speed-vs-budget lean; see resolve_weights()).
    jobs = sorted(state.jobs, key=lambda j: (effective_deadline(j, pol), j.created_at, j.id))
    for job in jobs:
        cands = _feasible_candidates(W, job, pol)
        if not cands:
            actions.append(Queue(job.id, "no feasible target meets the deadline now"))
            continue
        best = min(cands, key=lambda c: _score(c, cands, pol.weights))
        t = best.target
        if best.provision:
            W.provisioned[t.id] += 1
            actions.append(Provision(t.id, job.id, t.tier, best.est_cost,
                                     reason=f"burst {t.tier.name}: no cheaper running room"))
        else:
            W.free[t.id] = _sub(W.free[t.id], job.need)
            W.admitted_to.add(t.id)
            actions.append(Admit(job.id, t.id, best.est_cost,
                                 reason="run on existing " + t.tier.name))

    # Deprovision hysteresis: an elastic, non-local, currently-running target that
    # took no job this cycle, is past its min-uptime, and has no remaining queued
    # demand it could serve — drain it so idle burst capacity does not bleed money.
    queued_kinds_selectors = [j for j in jobs
                              if any(isinstance(a, Queue) and a.job_id == j.id for a in actions)]
    for t in state.targets:
        if not (t.running and t.elastic and t.tier != Tier.LOCAL):
            continue
        if t.id in W.admitted_to:
            continue
        if (state.now - t.up_since) < pol.min_uptime_s:
            continue                                    # anti-thrash: too fresh to kill
        could_serve = any(_selector_matches(t, j.selector) and _fits(j.need, t.capacity)
                          for j in queued_kinds_selectors)
        if could_serve:
            continue
        actions.append(Deprovision(t.id, "idle burst worker, no demand"))

    return FleetPlan(tuple(actions))


# --- weight resolution (time-of-day base -> auto pressure -> manual override) ---
# Named profiles. Peak leans speed (meet deadlines, burst freely); off-peak leans
# budget (prefer local, wait, avoid paid tiers).
PROFILE_PEAK = Weights(resource=1.0, budget=0.5, speed=2.0)
PROFILE_OFFPEAK = Weights(resource=1.5, budget=2.0, speed=0.5)
PROFILE_BALANCED = Weights(resource=1.0, budget=1.0, speed=1.0)


@dataclass(frozen=True)
class WeightPolicy:
    """How the shared weights vary over time. Layered, last-wins:
    time-of-day base -> auto pressure adjust -> manual override."""
    peak_hours: frozenset = field(default_factory=lambda: frozenset(range(9, 22)))  # 09:00-21:59 UTC
    peak: Weights = PROFILE_PEAK
    offpeak: Weights = PROFILE_OFFPEAK
    manual: Optional[Weights] = None         # explicit pin beats everything
    # auto-adjust gains (0 disables an axis)
    cost_pressure_gain: float = 1.0          # over-target spend -> more budget weight
    deadline_risk_gain: float = 1.0          # jobs near their deadline -> more speed weight
    deadline_risk_window_s: float = 300.0    # a job is "at risk" within this of its deadline


def _hour_of_day(now: float) -> int:
    return int((now // 3600) % 24)


def resolve_weights(state: FleetState, wpol: Optional[WeightPolicy] = None,
                    scheduler: Optional[SchedulerPolicy] = None) -> Weights:
    """Pure: compute the effective weights for ``state``. The broker calls this to
    fill ``SchedulerPolicy.weights`` before ``schedule()``.

    Manual override wins outright. Otherwise a time-of-day base is nudged by pressure:
    over-target spend (soft budget, §4 of the design) raises the budget weight; jobs
    near their deadline raise the speed weight. Budget never blocks — it only leans."""
    wp = wpol or WeightPolicy()
    if wp.manual is not None:
        return wp.manual
    base = wp.peak if _hour_of_day(state.now) in wp.peak_hours else wp.offpeak

    budget = base.budget + wp.cost_pressure_gain * state.ledger.pressure()

    pol = scheduler or SchedulerPolicy()
    at_risk = sum(1 for j in state.jobs
                  if effective_deadline(j, pol) - state.now <= wp.deadline_risk_window_s)
    risk_frac = (at_risk / len(state.jobs)) if state.jobs else 0.0
    speed = base.speed + wp.deadline_risk_gain * risk_frac

    return Weights(resource=base.resource, budget=budget, speed=speed)
