"""placement_default.py — the fleet problem builder and `default-v1`.

`problem_from_fleet` is the HARD-CONSTRAINT stage: it asks `fleet_rank.rank` which
candidates are eligible at all (serves the kind, in region policy, meets the
requirement, fresh, not leased, not held back as cold) and stamps the answer on
each candidate with the rule that decided it. `default_v1` is the SOFT stage: the
existing `fleet_rank` ordering (readiness, region / measured distance, load,
preference such as native streaming, ties on target id), unchanged, behind the
`placement/v1` interface. fleet_rank.py is not rewritten; it IS the default.

A candidate's `facts` carry the node row under `fleet.node`, so the logged problem
is enough to replay the default offline. `context` carries the vantage, the
region policy and the link table slice distance needs (`fleet.hosts`), under
namespaced keys.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from . import placement
from .fleet_rank import rank


def _target_id(node: dict) -> str:
    peer = node.get("peer", "")
    return peer[: -len("/livestack")] if peer.endswith("/livestack") else peer


def _view_without(view: dict, exclude) -> dict:
    if not exclude:
        return view
    drop = {e.rstrip("/") for e in exclude}
    return {**view, "hosts": {
        hid: {**h, "nodes": [n for n in h.get("nodes") or []
                             if str(n.get("peer") or "").rsplit("/livestack", 1)[0].rstrip("/") not in drop]}
        for hid, h in (view.get("hosts") or {}).items()}}


def problem_from_fleet(view: dict, kind: str, *, vantage: str, require: Optional[dict],
                       prefer: list, regions: Optional[List[str]], expected_tokens: Optional[int],
                       exclude=(), now: Optional[float] = None) -> "tuple[dict, dict]":
    """(problem, rank_result). `rank_result` is fleet_rank's own answer, returned
    so a caller can keep its explanatory record exactly as it was."""
    view = _view_without(view, exclude)
    result = rank(view, kind, vantage=vantage, require=require, prefer=prefer, now=now,
                  expected_tokens=expected_tokens, allow_regions=regions)
    nodes = {}
    for hid, h in (view.get("hosts") or {}).items():
        for n in h.get("nodes") or []:
            nodes[_target_id(n)] = (hid, n)
    cands = []
    for c in result["candidates"]:
        hid, node = nodes.get(c.target_id, (c.host_id, {}))
        eligible = c.outcome != "filtered"
        cands.append({
            "id": c.target_id, "eligible": eligible,
            **({} if eligible else {"ineligible_reason": c.reason}),
            "facts": {"fleet.host_id": hid, "fleet.node": node, "ready": c.ready,
                      "region": c.region, "distance_ms": c.distance_ms,
                      "distance_band": c.distance_band, "load": c.load,
                      "inventory": c.inventory},
        })
    hosts = {hid: {"links": h.get("links") or {},
                   "region": next(((n.get("region") or "").strip().lower() or None
                                   for n in h.get("nodes") or [] if n.get("region")), None)}
             for hid, h in (view.get("hosts") or {}).items()}
    problem = placement.make_problem(
        kind, {"require": require or {}, "prefer": prefer or [], "regions": list(regions or []),
               "expected_tokens": expected_tokens},
        cands,
        {"fleet.vantage": vantage, "fleet.hosts": hosts, "fleet.relays": view.get("relays") or {},
         "fleet.generated_at": view.get("generated_at")},
        now=now)
    return problem, result


def _rebuild_view(problem: dict) -> dict:
    ctx = problem.get("context") or {}
    hosts: Dict[str, dict] = {}
    for hid, meta in (ctx.get("fleet.hosts") or {}).items():
        stub = [{"region": meta["region"]}] if meta.get("region") else []
        hosts[hid] = {"links": meta.get("links") or {}, "nodes": stub}
    for c in problem.get("candidates") or []:
        f = c.get("facts") or {}
        hid = f.get("fleet.host_id")
        node = f.get("fleet.node")
        if hid is None or not node:
            continue
        hosts.setdefault(hid, {"links": {}, "nodes": []})
        hosts[hid]["nodes"] = [n for n in hosts[hid]["nodes"] if n.get("peer")] + [node]
    return {"generated_at": ctx.get("fleet.generated_at"), "hosts": hosts,
            "relays": ctx.get("fleet.relays") or {}}


def default_v1(problem: dict) -> dict:
    """The deterministic default. Unknown facts and context keys are ignored."""
    req = problem.get("request") or {}
    result = rank(_rebuild_view(problem), problem["kind"],
                  vantage=(problem.get("context") or {}).get("fleet.vantage") or "direct",
                  require=req.get("require") or None, prefer=req.get("prefer") or [],
                  now=problem.get("ts"), expected_tokens=req.get("expected_tokens"),
                  allow_regions=req.get("regions") or None)
    allowed = set(placement.eligible_ids(problem))
    rows = []
    for t in result["targets"]:
        if t["target_id"] in allowed:
            rows.append({"id": t["target_id"], "score": -float(t["rank"]),
                         "explain": {"rank": t["rank"], "reason": t["reason"],
                                     "distance_band": t.get("distance_band"),
                                     "distance_ms": t.get("distance_ms"),
                                     "load": t.get("load"), "preferences": t.get("preferences"),
                                     "finish_s": t.get("finish_s")}})
    return {"ranking": rows}


placement.register(placement.DEFAULT_RANKER, default_v1, trusted=True)
