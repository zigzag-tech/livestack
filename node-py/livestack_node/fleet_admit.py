"""fleet_admit.py — "throw a task at the fleet and it knows where to run it".

A **pure function** from a fleet view + a request to a decision, the same
discipline as `planner.py`, `membership.py`, `fleet_rank.py` and
`fleet_scheduler.py`. No I/O, injectable clock.

It is a thin translation layer on purpose. `fleet_scheduler.schedule()` already
does cost, deadline and elastic-burst reasoning and is already pure; what it
lacked was a `FleetState` built from something that knows where the machines
actually are. This builds one, and adds exactly one term — measured distance —
leaving cost/deadline/burst untouched and the RunPod and Aliyun tiers available
for elastic overflow exactly as designed.

**Two brains, and they stay separate.** The answer here is "go to host H, node
N". Whether `align` is resident on N, and whether loading it would evict
something, remains that host's own broker's decision — the caller's request to N
triggers N's `manager.ensure` → its host broker's `/admit`, exactly as today.
A fleet broker that reached into a host's residency would be the second master
this whole design exists not to have.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Mapping, Optional

from .fleet_rank import distance_to, load_value
from .fleet_scheduler import (
    Admit, CostModel, FleetPlan, FleetState, Job, Queue, SchedulerPolicy, Sla,
    Target, Tier, schedule,
)
from .ledger import Candidate, distance_band

SLA_BY_NAME = {"interactive": Sla.INTERACTIVE, "normal": Sla.NORMAL, "batch": Sla.BATCH}

# What one node is assumed able to serve concurrently when nothing says
# otherwise. It is a CONCURRENCY ceiling, not a memory one: memory is the host
# broker's business and it is the only thing that can see it.
DEFAULT_CONCURRENCY = 4.0


def targets_from_view(view: dict, kind: str, vantage: str = "direct",
                      concurrency: float = DEFAULT_CONCURRENCY) -> tuple:
    """Every fleet node that could serve `kind`, as scheduler `Target`s, plus the
    rows for the ones that could not and why.

    Free capacity is `concurrency - in_flight`, floored at zero. A node with NO
    load opinion is credited with its full concurrency rather than none: absent
    means no opinion, and refusing to schedule anything that has not reported is
    how a fleet strands its quietest engines. The uncertainty is recorded in the
    candidate's reason instead, where a later reader can see it.
    """
    targets: List[Target] = []
    rejected: List[Candidate] = []
    for host_id, host in sorted((view.get("hosts") or {}).items()):
        for node in host.get("nodes") or []:
            peer = node.get("peer", "")
            base = peer[: -len("/livestack")] if peer.endswith("/livestack") else peer
            dist = distance_to(view, host_id, node, vantage)
            load = node.get("load") if isinstance(node.get("load"), dict) else None
            common = dict(
                id=base, host_id=host_id, device_id=node.get("device_id"),
                state=node.get("state", "mia"), ready=bool(node.get("ready")),
                distance_ms=dist, distance_band=distance_band(dist), load=load,
                inputs_at=view.get("generated_at"),
            )
            kinds = {k for k in (node.get("kinds") or []) if k}
            kinds |= {u.get("kind") for u in (node.get("units") or []) if u.get("kind")}
            resident = any(u.get("kind") == kind and u.get("resident")
                           for u in (node.get("units") or []))
            if kind and kind not in kinds:
                rejected.append(Candidate(outcome="filtered",
                                          reason=f"filtered: does not host {kind}",
                                          **common))
                continue
            if node.get("state") != "fresh":
                rejected.append(Candidate(
                    outcome="filtered",
                    reason=f"filtered: state={node.get('state')}"
                           + (f" ({node['last_error'][:80]})" if node.get("last_error") else ""),
                    **common))
                continue
            if not node.get("ready"):
                rejected.append(Candidate(
                    outcome="filtered",
                    reason=f"filtered: not ready ({node.get('detail') or 'no detail'})",
                    **common))
                continue
            in_flight = (load or {}).get("in_flight")
            used = float(in_flight) if isinstance(in_flight, (int, float)) else 0.0
            free = max(0.0, concurrency - used)
            if free <= 0:
                rejected.append(Candidate(
                    outcome="filtered",
                    reason=f"filtered: saturated (in_flight={in_flight} of {concurrency:.0f})",
                    **common))
                continue
            labels = dict(node.get("labels") or {})
            labels.setdefault("host_id", host_id)
            if node.get("device_id"):
                labels.setdefault("device_id", node["device_id"])
            targets.append(Target(
                id=base, host_id=host_id, tier=Tier.LOCAL,
                capacity={"concurrency": free},
                cost=CostModel(),          # a machine we already own is sunk cost
                running=True, elastic=False, labels=labels,
                distance_ms=dist, utilization=load_value(load),
            ))
            # Kept alongside so the ledger can show what the scheduler saw.
            rejected.append(Candidate(
                outcome="ranked", resident=resident,
                reason=_considered_reason(common, free, concurrency, resident, load),
                **common))
    return tuple(targets), rejected


def _considered_reason(common: dict, free: float, concurrency: float,
                       resident: bool, load: Optional[dict]) -> str:
    bits = [f"band{common['distance_band']}"]
    if common["distance_ms"] is not None:
        bits[0] += f" ({common['distance_ms']:.0f}ms)"
    lv = load_value(load)
    if lv is None:
        # Said out loud rather than folded into a number: crediting a silent node
        # with full capacity is a choice, and a reader has to be able to see that
        # it was made.
        bits.append(f"load: no opinion, credited {concurrency:.0f} slot(s)")
    else:
        bits.append(f"free {free:.0f}/{concurrency:.0f}"
                    + (f", in_flight={load.get('in_flight')}" if load.get("in_flight") is not None else "")
                    + (f", pressure={load.get('pressure')}" if load.get("pressure") is not None else ""))
    bits.append("resident" if resident else "not resident")
    return "; ".join(bits)


def admit(view: dict, *, kind: str, sla: str = "normal", owner: str = "consumer",
          selector: Optional[Mapping[str, str]] = None,
          locality_host: Optional[str] = None,
          vantage: str = "direct",
          estimate_s: float = 60.0,
          concurrency: float = DEFAULT_CONCURRENCY,
          policy: Optional[SchedulerPolicy] = None,
          usage: Optional[Mapping[str, int]] = None,
          now: Optional[float] = None) -> Dict[str, Any]:
    """Decide where one job should run. Returns the grant and the full candidate
    set, winner and losers alike, each with the reason it landed where it did."""
    now = time.time() if now is None else now
    targets, rows = targets_from_view(view, kind, vantage=vantage, concurrency=concurrency)
    job = Job(
        id=f"{kind}-{int(now * 1000)}", kind=kind, owner=owner,
        need={"concurrency": 1.0},
        est_duration_s=estimate_s,
        sla=SLA_BY_NAME.get(str(sla).lower(), Sla.NORMAL),
        created_at=now,
        selector=dict(selector or {}),
        locality_host=locality_host,
    )
    plan: FleetPlan = schedule(
        FleetState(targets=targets, jobs=(job,), now=now,
                   usage=dict(usage or {})), policy)

    chosen_id = next((a.target_id for a in plan.actions
                      if isinstance(a, Admit) and a.job_id == job.id), None)
    deferred = next((a for a in plan.actions
                     if isinstance(a, Queue) and a.job_id == job.id), None)

    by_id = {t.id: t for t in targets}
    out_rows: List[Candidate] = []
    rank = 0
    for c in rows:
        if c.outcome == "filtered":
            out_rows.append(c)
            continue
        rank += 1
        if c.id == chosen_id:
            out_rows.append(Candidate(**{**c.__dict__, "outcome": "chosen", "rank": 1,
                                         "reason": f"chosen: {c.reason}"}))
        else:
            out_rows.append(Candidate(**{**c.__dict__, "outcome": "ranked", "rank": rank + 1,
                                         "reason": f"feasible, not chosen: {c.reason}"}))

    target = by_id.get(chosen_id) if chosen_id else None
    node = f"{chosen_id}/livestack" if chosen_id else None
    refused_for_quota = bool(
        deferred and str(getattr(deferred, "reason", "")).startswith("account quota"))
    return {
        "granted": chosen_id is not None,
        "refused": "account_quota" if refused_for_quota else None,
        "kind": kind,
        "sla": sla,
        "vantage": vantage,
        "generated_at": now,
        "target": ({"host_id": target.host_id,
                    "device_id": target.labels.get("device_id"),
                    "node": node,
                    "target_id": chosen_id} if target else None),
        "reason": _why(chosen_id, out_rows, deferred, kind, vantage),
        "candidates": out_rows,
        "plan": plan.summary() if hasattr(plan, "summary") else None,
    }


def _why(chosen_id, rows, deferred, kind, vantage) -> str:
    if chosen_id is None:
        detail = getattr(deferred, "reason", None) if deferred else None
        if detail and detail.startswith("account quota"):
            # A quota refusal is a DIFFERENT answer from "the fleet is full",
            # and a caller that cannot tell them apart retries forever against a
            # fleet that will never say yes. Name the feasible targets it could
            # have had, so the record shows the room existed.
            feasible = sum(1 for r in rows if r.outcome == "ranked")
            return (f"refused: {detail}; {feasible} target(s) could otherwise "
                    f"have run {kind}")
        return (f"no fleet target can run {kind} from {vantage}"
                + (f": {detail}" if detail else "")
                + f"; {sum(1 for r in rows if r.outcome == 'filtered')} candidate(s) filtered")
    winner = next(r for r in rows if r.id == chosen_id)
    losers = [r for r in rows if r.outcome == "ranked"]
    tail = ""
    if losers:
        # Name the nearest loser specifically. "It should have gone to X" is a
        # question about ONE alternative, so the reason has to name one.
        near = min(losers, key=lambda r: (r.distance_ms if r.distance_ms is not None else 1e9))
        tail = f"; {near.id} lost on {near.reason.replace('feasible, not chosen: ', '')}"
    return f"{chosen_id} ({winner.reason.replace('chosen: ', '')}){tail}"
