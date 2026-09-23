"""fleet_ops_api.py — the three routes that let something else drive the fleet.

``/fleet/admit`` answers one question — *where should this one job run right
now* — and answers it by doing. It is the right shape for a caller with a job
and the wrong shape for a supervision loop, which needs to see the whole plan,
decide, and act on one action at a time with a durable record of each.

So: three routes, versioned, with a strict division of labour.

* ``POST /fleet/plan`` **reads**. It reserves nothing, holds nothing, and
  changes nothing. Call it twice and get the same answer. It reports the
  actions the pure scheduler produced, the pools that were excluded and why,
  the reservations already outstanding, and — the part a wrapper would quietly
  destroy — the places where the fleet's own inputs were UNKNOWN rather than
  zero.
* ``POST /fleet/operations`` **spends**. It claims first, durably, and only then
  calls a provider. The reply can be lost without costing anything, because the
  claim was written before the money was.
* ``GET /fleet/operations/{id}`` **observes**. It is what a gate reads, and it
  reports the correlated facts rather than the HTTP status of the dispatch.

Kept out of ``hostd.py`` because ``hostd`` is already the place every route
eventually lands, and a control plane's routes want to be readable next to the
lifecycle they drive rather than next to the residency dashboard.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from .fleet_admit import DEFAULT_CONCURRENCY, SLA_BY_NAME, targets_from_view
from .fleet_operations import (
    ClaimRefused, DrainRefused, OperationStore, UnknownOperation,
)
from .fleet_pools import Pool, pool_targets, spec_for
from .fleet_scheduler import (
    Admit, Deprovision, FleetState, Job, Provision, Queue, SchedulerPolicy,
    Target, Tier, schedule,
)
from .fleet_workers import WorkerProvider, run_provision

API_VERSION = "v1"
#: How old a plan may be when an operation quotes it. A plan is a photograph of
#: a fleet that keeps moving; past this the caller should take another one.
#: Not a safety property — the claim is what makes spending safe — but it stops
#: a loop that wedged for an hour from acting on an hour-old world.
DEFAULT_PLAN_MAX_AGE_S = 120.0


def kind_labels(kinds) -> Dict[str, str]:
    """``{"kind.llm": "1", ...}`` — one label per capability a target serves.

    Kinds live in LABELS rather than in a field because that is how the pure
    scheduler already gates: ``Job.selector`` must be a subset of
    ``Target.labels``. Expressing it this way lets one node with one shared
    concurrency budget serve several kinds, which a per-kind target list cannot
    — it would hand the same free slot out twice.
    """
    return {f"kind.{k}": "1" for k in kinds if k}


def plan_targets(view: Mapping[str, Any], kinds: Tuple[str, ...], *,
                 pools: Tuple[Pool, ...] = (),
                 running_instances: Optional[Mapping[str, int]] = None,
                 vantage: str = "direct", owner: str = "",
                 concurrency: float = DEFAULT_CONCURRENCY,
                 allow_regions: Tuple[str, ...] = (),
                 ) -> Tuple[Tuple[Target, ...], List[dict], List[dict]]:
    """Every target a plan may use: running fleet nodes and rentable pools.

    Returns ``(targets, excluded, uncertainty)``. The last two are not
    decoration. ``excluded`` distinguishes "there was no capacity" from "your
    region policy removed every pool", which look identical in the actions and
    want opposite responses. ``uncertainty`` names each node whose free capacity
    was assumed rather than measured — ``targets_from_view`` credits a silent
    node with its full concurrency, which is the right default and a terrible
    thing to forget you did.
    """
    merged: Dict[str, Target] = {}
    excluded: List[dict] = []
    uncertainty: List[dict] = []
    seen_exclusions = set()
    for kind in kinds:
        local, rows = targets_from_view(view, kind, vantage=vantage,
                                        concurrency=concurrency, owner=owner)
        for t in local:
            prior = merged.get(t.id)
            labels = {**dict(t.labels), **kind_labels([kind])}
            if prior is not None:
                labels = {**dict(prior.labels), **labels}
            merged[t.id] = replace(t, labels=labels)
        for c in rows:
            if c.outcome == "filtered":
                key = (c.id, c.reason)
                if key not in seen_exclusions:
                    seen_exclusions.add(key)
                    excluded.append({"target_id": c.id, "kind": kind,
                                     "reason": c.reason})
            elif c.load is None and not any(
                    u["target_id"] == c.id for u in uncertainty):
                # Said out loud: the planner used a number nobody reported.
                uncertainty.append({
                    "target_id": c.id, "field": "in_flight",
                    "assumed": concurrency,
                    "reason": (f"{c.id} reported no load; credited its full "
                               f"{concurrency:.0f} slot(s). Free capacity here is "
                               f"assumed, not measured.")})
    elastic, pool_excluded = pool_targets(
        pools, kinds=kinds, running_instances=running_instances,
        allow_regions=allow_regions)
    for t in elastic:
        pool = next(p for p in pools if p.id == t.id)
        # A pool that declares no kinds serves whatever was asked for; one that
        # declares some serves exactly those.
        # Label with the intersection: a pool that serves llm and asr, asked
        # about llm only, must not advertise asr capacity this plan never
        # checked it for.
        labels = {**dict(t.labels),
                  **kind_labels(tuple(k for k in kinds if not pool.kinds
                                      or k in pool.kinds) or pool.kinds)}
        merged[t.id] = replace(t, labels=labels)
    excluded.extend(pool_excluded)
    return tuple(merged.values()), excluded, uncertainty


def job_from_request(row: Mapping[str, Any], *, owner: str,
                     now: float) -> Job:
    """One caller-supplied queued job. ``job_id`` is the CALLER's, unchanged:
    the loop already named it, the ledger joins on it, and renaming it here
    would break every join the change exists to make possible."""
    kind = str(row.get("kind") or "")
    return Job(
        id=str(row.get("job_id") or f"{kind}-{int(now * 1000)}"),
        kind=kind, owner=owner,
        need={"concurrency": float(row.get("need") or 1.0)},
        created_at=float(row.get("created_at") or now),
        sla=SLA_BY_NAME.get(str(row.get("sla") or "normal").lower()),
        deadline=(float(row["deadline_at"]) if row.get("deadline_at") else None),
        est_duration_s=float(row.get("est_duration_s") or 60.0),
        selector={**{str(k): str(v) for k, v in (row.get("selector") or {}).items()},
                  **kind_labels([kind])},
        locality_host=row.get("locality_host"))


def policy_digest(policy: SchedulerPolicy, pools: Tuple[Pool, ...]) -> str:
    """A short digest of everything a plan's arithmetic depended on that is NOT
    the fleet view: the weights, the ceilings, the pools and their prices.

    It is what makes a stale plan detectable. The view moves constantly and that
    is normal; the POLICY moving under a plan means the plan was computed
    against rules that no longer apply, and acting on it would spend money by
    yesterday's decision.
    """
    blob = json.dumps({
        "weights": [policy.weights.resource, policy.weights.budget,
                    policy.weights.speed],
        "quotas": dict(sorted(policy.account_quotas.items())),
        "max_per_account": policy.max_concurrent_per_account,
        "pools": sorted((p.id, p.provider, p.tier.name, p.region,
                         p.instance_type, p.cost_per_hour, p.max_instances)
                        for p in pools),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:12]


def serialize_action(a) -> dict:
    if isinstance(a, Admit):
        return {"type": "admit", "job_id": a.job_id, "target_id": a.target_id,
                "est_cost": a.est_cost, "reason": a.reason}
    if isinstance(a, Provision):
        return {"type": "provision", "job_id": a.job_id, "target_id": a.target_id,
                "tier": a.tier.name, "est_cost": a.est_cost, "reason": a.reason}
    if isinstance(a, Queue):
        return {"type": "queue", "job_id": a.job_id, "reason": a.reason}
    if isinstance(a, Deprovision):
        return {"type": "deprovision", "target_id": a.target_id, "reason": a.reason}
    raise ValueError(f"unserializable action {a!r}")


def build_plan(view: Mapping[str, Any], job_rows, *, owner: str,
               policy: SchedulerPolicy, pools: Tuple[Pool, ...],
               store: OperationStore, usage: Mapping[str, int],
               allow_regions: Tuple[str, ...] = (), vantage: str = "direct",
               now: Optional[float] = None) -> dict:
    """The whole answer for ``POST /fleet/plan``. A pure-ish read: it touches the
    operation store only to COUNT what is already reserved, and reserves nothing
    itself."""
    now = time.time() if now is None else now
    jobs = tuple(job_from_request(r, owner=owner, now=now) for r in job_rows)
    kinds = tuple(dict.fromkeys(j.kind for j in jobs if j.kind))
    active = store.active()
    running_instances: Dict[str, int] = {}
    for op in active:
        running_instances[op.target_id] = running_instances.get(op.target_id, 0) + 1
    targets, excluded, uncertainty = plan_targets(
        view, kinds, pools=pools, running_instances=running_instances,
        vantage=vantage, owner=owner, allow_regions=allow_regions)
    # Usage the scheduler must see: leases held PLUS creates in flight. Leaving
    # the second out is how an owner at its ceiling is handed more machines.
    combined = dict(usage)
    for o, n in store.pending_usage().items():
        combined[o] = combined.get(o, 0) + n
    plan = schedule(FleetState(targets=targets, jobs=jobs, now=now,
                               usage=combined), policy)
    digest = policy_digest(policy, pools)
    generated_at = float(view.get("generated_at") or now)
    return {
        "api": API_VERSION,
        "plan_version": f"{API_VERSION}.{digest}.{int(generated_at)}",
        "generated_at": generated_at,
        "jobs": [{"job_id": j.id, "kind": j.kind, "owner": j.owner,
                  "sla": j.sla.name.lower(), "created_at": j.created_at,
                  "deadline_at": j.deadline, "selector": dict(j.selector)}
                 for j in jobs],
        "pools": [{"target_id": t.id, "tier": t.tier.name,
                   "provider": t.labels.get("provider"),
                   "region": t.labels.get("region"),
                   "running_instances": t.running_instances,
                   "max_instances": t.max_instances,
                   "cost_per_hour": t.cost.per_hour,
                   "provision_latency_s": t.provision_latency_s}
                  for t in targets if t.elastic],
        "excluded": excluded,
        # What the plan did NOT know. Preserved rather than defaulted away: a
        # target whose capacity was assumed is not the same as one whose
        # capacity was measured, and only the plan can still tell the difference.
        "uncertainty": uncertainty,
        "reservations": [{"operation_id": op.operation_id, "job_id": op.job_id,
                          "target_id": op.target_id, "owner": op.owner,
                          "state": op.state, "node_id": op.node_id,
                          "observability_degraded": op.observability_degraded}
                         for op in active],
        "policy": {"digest": digest,
                   "weights": {"resource": policy.weights.resource,
                               "budget": policy.weights.budget,
                               "speed": policy.weights.speed},
                   "max_concurrent_per_account": policy.max_concurrent_per_account,
                   "account_quotas": dict(policy.account_quotas)},
        "actions": [serialize_action(a) for a in plan.actions],
    }


def plan_is_current(plan_version: str, *, policy: SchedulerPolicy,
                    pools: Tuple[Pool, ...], now: float,
                    max_age_s: float = DEFAULT_PLAN_MAX_AGE_S) -> Optional[str]:
    """Why this plan may no longer be acted on, or None."""
    try:
        api, digest, stamp = str(plan_version).split(".", 2)
    except ValueError:
        return f"plan_version {plan_version!r} is not a {API_VERSION} plan version"
    if api != API_VERSION:
        return f"plan_version is {api}, this broker speaks {API_VERSION}"
    if digest != policy_digest(policy, pools):
        return ("the policy or the pool set changed since this plan was computed; "
                "take a new plan")
    age = now - float(stamp)
    if age > max_age_s:
        return f"the plan is {age:.0f}s old (limit {max_age_s:.0f}s); take a new one"
    return None


# --- the spending half ------------------------------------------------------
class ActionRefused(Exception):
    """A refusal with an HTTP status and a sentence. Every refusal on this path
    says which of the four gates fired — stale plan, unknown pool, quota/region,
    or a busy node — because "409" alone sends a caller retrying a thing that
    will never succeed."""

    def __init__(self, status: int, detail: str):
        super().__init__(detail)
        self.status, self.detail = status, detail


def provision(action: Mapping[str, Any], *, store: OperationStore,
              pools: Tuple[Pool, ...], providers: Mapping[str, WorkerProvider],
              owner: str, principal: Optional[str], plan_version: str,
              idempotency_key: str, policy: SchedulerPolicy,
              usage: Mapping[str, int], announce_env: Mapping[str, str],
              allow_regions: Tuple[str, ...] = (),
              allow_unknown_region: bool = False,
              spawn: Optional[Callable[[Callable[[], None]], None]] = None,
              log: Callable[[str], None] = lambda *_: None,
              now: Optional[float] = None) -> dict:
    """Claim, then create. In that order, always.

    The claim is a synchronous, durable write; the provider call is not. That
    asymmetry is deliberate: this function can lose its reply, its process or
    its network the instant after it returns and the operation is still on disk,
    still counted against its owner's quota, and still reconcilable. A version
    that created first and recorded afterwards would, in the same instant, have
    rented a machine nobody can find.
    """
    target_id = str(action.get("target_id") or "")
    pool = next((p for p in pools if p.id == target_id), None)
    if pool is None:
        raise ActionRefused(409, f"no elastic pool named {target_id!r} is "
                                 f"configured on this broker")
    provider = providers.get(pool.provider)
    if provider is None:
        raise ActionRefused(
            501, f"pool {pool.id} names provider {pool.provider!r} and this "
                 f"broker has no adapter for it; nothing was claimed")
    try:
        op = store.claim(
            job_id=str(action.get("job_id") or ""), owner=owner,
            principal=principal, target_id=pool.id,
            kind=str(action.get("kind") or ""), idempotency_key=idempotency_key,
            tier=pool.tier.name, provider=pool.provider, region=pool.region,
            plan_version=plan_version, usage=usage, policy=policy,
            allow_regions=allow_regions,
            allow_unknown_region=allow_unknown_region, now=now)
    except ClaimRefused as e:
        # 409, not 429: the caller is not being rate-limited, it is being told
        # its plan cannot be acted on. The reason says which rule fired.
        raise ActionRefused(409, e.operation.reason or str(e))
    if op.state != "creating":
        # A replayed idempotency key. Return what already happened rather than
        # doing it again — that is what the key is for.
        return op.to_dict()

    spec = spec_for(pool, announce_env=announce_env)

    def go():
        try:
            run_provision(store, op, provider, spec, log=log)
        except BaseException as exc:  # noqa: BLE001 — a thread that dies silently
            # is the exact failure this codebase refuses to ship. `run_provision`
            # already records every outcome it can classify; anything reaching
            # here is a defect, and it must be visible rather than lost.
            log(f"[fleet] operation {op.operation_id}: dispatch thread failed: "
                f"{exc}; the operation stays {store.get(op.operation_id).state} "
                f"and will be reconciled")
    (spawn or _thread)(go)
    return op.to_dict()


def _thread(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="fleet-provision", daemon=True).start()


def deprovision(action: Mapping[str, Any], *, store: OperationStore,
                providers: Mapping[str, WorkerProvider],
                busy: Callable[[str], Optional[str]],
                log: Callable[[str], None] = lambda *_: None,
                now: Optional[float] = None) -> dict:
    """Drain and release one node. ``busy(node_id)`` is the authority on whether
    it is empty, and it is called again under the store's writer lock.

    A ``Deprovision`` in a plan is a PROPOSAL. It was computed from a view, and
    between the view and this call a job can be admitted to the node. That is
    not a rare race; on a busy fleet it is the normal case, which is why the
    refusal here is ordinary rather than exceptional.
    """
    node_id = str(action.get("target_id") or "")
    op = store.for_node(node_id)
    if op is None:
        raise ActionRefused(
            409, f"this broker has no live operation for node {node_id!r}; it "
                 f"did not provision it and will not release it")
    try:
        released = store.release(op.operation_id, busy=lambda: busy(node_id),
                                 now=now)
    except DrainRefused as e:
        raise ActionRefused(409, f"{node_id} is not drainable: {e.reason}")
    provider = providers.get(op.provider or "")
    if provider is not None and op.provider_instance_id:
        try:
            provider.terminate(op.provider_instance_id)
        except BaseException as exc:  # noqa: BLE001 — the release already applied
            # Loud, and reported in the record: a released operation whose
            # instance is still running is money burning with nobody watching,
            # and it must not look the same as a clean teardown.
            log(f"[fleet] {op.operation_id}: RELEASED but terminating "
                f"{op.provider_instance_id} failed: {exc}. The instance may "
                f"still be billing; reap it by hand.")
            return {**released.to_dict(), "teardown": "failed",
                    "teardown_error": str(exc)[:400]}
    return {**released.to_dict(), "teardown": "ok"}


def providers_from_env(pools: Tuple[Pool, ...],
                       log: Callable[[str], None] = lambda *_: None
                       ) -> Dict[str, WorkerProvider]:
    """One adapter per provider the configured pools actually name.

    Built from the pools rather than from a fixed list, so a broker with no
    Aliyun pool never needs an Aliyun credential — and a pool naming a provider
    with no adapter is reported at startup rather than at the first burst.
    """
    from .fleet_workers import AliyunEcsWorkerProvider
    builders = {"aliyun": AliyunEcsWorkerProvider}
    out: Dict[str, WorkerProvider] = {}
    for name in sorted({p.provider for p in pools}):
        builder = builders.get(name)
        if builder is None:
            log(f"[fleet] pools name provider {name!r} and there is no adapter "
                f"for it; those pools can be planned but never provisioned")
            continue
        out[name] = builder()
    return out
