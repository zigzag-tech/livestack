"""fleet_rank.py — where should this request START, given the whole fleet.

A **pure function** over a fleet view: no I/O, no ambient clock, time passed in.
Same discipline as `planner.py`, `membership.py` and `fleet_scheduler.py`, and
for the same reason — the interesting behaviour is the ordering, and ordering
that needs a socket to test is ordering nobody tests.

What it is NOT, which is most of the design:

* **Not a replacement for the client picker.** Local probing, quarantine and
  failover are what keep dictation alive through a dead route. This only keeps a
  client from *starting* on the wrong continent; the picker re-measures the fine
  detail itself, which is why distances are bucketed into BANDS rather than
  compared as raw milliseconds. Bands also keep the order stable under jitter,
  which raw millis are not.
* **Not a source of region.** Region is operator policy and lives on the grant.
  The caller applies its own region filter — the hub knows which regions an
  account is allowed; the fleet broker must never decide policy.
* **Not authoritative.** The response carries `generated_at` and `ttl_s`;
  consumers discard past the TTL. A stale ranking is worse than none, and costs
  at most one bad first guess, because the picker still probes and fails over.

The order is lexicographic and each level is there for a reason:

1. **distance band from the vantage.** The first-order fact. A distant idle GPU
   does not repay the round trip to reach it.
2. **load, ascending, within a band.** Queue depth is primary and pressure may
   only RAISE it — device pressure is dominated by the resident model's weights,
   not by queued work, so two idle engines holding the same model report
   byte-identical pressure and cannot break a tie between themselves. Six
   concurrent ASR requests moved pressure by under half a percent. This is the
   same rule the client picker uses; they must not diverge.
   A candidate with NO OPINION on load sorts after every candidate that has one —
   silence is not idleness — but still ahead of anything in a worse band.
3. **`target_id`**, so the order is total and deterministic.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .ledger import distance_band

DEFAULT_TTL_S = 60.0

# Sort order of the bands. `unknown` last: a distance we failed to measure is not
# evidence of nearness, and treating it as such is how a fleet sends work across
# an ocean on the strength of having no reading.
_BAND_ORDER = {"<50": 0, "<200": 1, "<600": 2, ">=600": 3, "unknown": 4}


@dataclass(frozen=True)
class RankedTarget:
    """One candidate, with the inputs used to place it.

    Carries `outcome`/`reason` for every row — including the ones filtered out —
    because this same list is what the decision ledger records, and a filtered
    candidate has to be a ROW with a reason rather than an omission. The absent
    row is exactly what stops a later reader from saying "it should have gone
    to X".
    """
    target_id: str
    node: str
    host_id: Optional[str]
    device_id: Optional[str]
    state: str
    ready: bool
    distance_ms: Optional[float]
    distance_band: str
    load: Optional[Dict[str, Any]]
    inputs_at: Optional[float]
    outcome: str                       # chosen | ranked | filtered
    reason: str
    rank: Optional[int] = None

    def to_wire(self) -> dict:
        return {"target_id": self.target_id, "node": self.node,
                "host_id": self.host_id, "device_id": self.device_id,
                "distance_ms": self.distance_ms,
                "distance_band": self.distance_band,
                "load": self.load, "rank": self.rank, "reason": self.reason}


def load_value(load: Optional[Dict[str, Any]]) -> Optional[float]:
    """Fold a node's load report into one 0..1 number, or None for no opinion.

    Queue depth normalized (soft-saturating at 8, roughly where a single consumer
    GPU stops being interactive) and pressure taking the MAX of the two — so
    pressure can raise the estimate but never mask a queue. Identical to the
    client picker's rule; a fleet that ordered by one measure while the client
    ordered by another would spend its time undoing itself.
    """
    if not isinstance(load, dict):
        return None
    p = load.get("pressure")
    p = float(p) if isinstance(p, (int, float)) and 0.0 <= float(p) <= 1.0 else None
    n = load.get("in_flight")
    q = min(1.0, float(n) / 8.0) if isinstance(n, (int, float)) and n >= 0 else None
    if p is None:
        return q
    if q is None:
        return p
    return max(p, q)


def distance_to(view: dict, node_host: Optional[str], node_row: dict,
                vantage: str) -> Optional[float]:
    """Measured distance from `vantage` to this node, or None for no opinion.

    * `direct` — the fleet broker's own `probe_ms`. Honest but single-vantage.
    * `host:<id>` — that host's own measured link row. Zero to itself: a node on
      the asking host is local, and no probe is needed to know that.
    * `relay:<id>` — the relay's own measured distance to the node's host, when
      the relay reports one.

    None means UNMEASURED, which sorts last. It never becomes a default: a
    default distance is a guess wearing a measurement's clothes.
    """
    if vantage in ("direct", "", None):
        ms = node_row.get("probe_ms")
        return float(ms) if isinstance(ms, (int, float)) else None
    if ":" not in vantage:
        return None
    scope, name = vantage.split(":", 1)
    if scope == "host":
        if node_host and node_host == name:
            return 0.0
        row = (view.get("hosts", {}).get(name, {}) or {}).get("links") or {}
        ms = row.get(node_host)
        return float(ms) if isinstance(ms, (int, float)) else None
    if scope == "relay":
        row = (view.get("relays", {}) or {}).get(name) or {}
        ms = (row.get("links") or {}).get(node_host)
        return float(ms) if isinstance(ms, (int, float)) else None
    return None


def _serves(node_row: dict, kind: str) -> bool:
    """Does this node host `kind`? Matches the node's own `kind` (`polyasr`) and
    its unit names (`asr`, `align`), because callers legitimately ask in both
    vocabularies and forcing one on them just moves the mapping somewhere it is
    less visible."""
    if not kind:
        return True
    kinds = {k for k in (node_row.get("kinds") or []) if k}
    kinds |= {u.get("kind") for u in (node_row.get("units") or []) if u.get("kind")}
    return kind in kinds


def rank(view: dict, kind: str, vantage: str = "direct",
         now: Optional[float] = None, ttl_s: float = DEFAULT_TTL_S) -> dict:
    """Order the fleet's nodes for one `(kind, vantage)`.

    Returns every candidate, winner and losers alike, each with the reason it
    landed where it did — the response takes the `ranked`/`chosen` rows and the
    ledger takes all of them.
    """
    now = time.time() if now is None else now
    rows: List[RankedTarget] = []
    eligible: List[RankedTarget] = []

    for host_id, host in sorted((view.get("hosts") or {}).items()):
        for node in host.get("nodes") or []:
            peer = node.get("peer", "")
            target_id = peer[: -len("/livestack")] if peer.endswith("/livestack") else peer
            dist = distance_to(view, host_id, node, vantage)
            band = distance_band(dist)
            load = node.get("load") if isinstance(node.get("load"), dict) else None
            common = dict(
                target_id=target_id, node=peer, host_id=host_id,
                device_id=node.get("device_id"), state=node.get("state", "mia"),
                ready=bool(node.get("ready")), distance_ms=dist,
                distance_band=band, load=load,
                inputs_at=view.get("generated_at"),
            )
            # Filters, in the order a reader would ask them. The FIRST rule that
            # eliminates a candidate is the reason recorded, because "it was also
            # far away" is not why it lost.
            if not _serves(node, kind):
                rows.append(RankedTarget(outcome="filtered",
                                         reason=f"filtered: does not host {kind}",
                                         **common))
                continue
            if node.get("state") != "fresh":
                rows.append(RankedTarget(
                    outcome="filtered",
                    reason=f"filtered: state={node.get('state')}"
                           + (f" ({node['last_error'][:80]})" if node.get("last_error") else ""),
                    **common))
                continue
            if not node.get("ready"):
                rows.append(RankedTarget(
                    outcome="filtered",
                    reason=f"filtered: not ready ({node.get('detail') or 'no detail'})",
                    **common))
                continue
            eligible.append(RankedTarget(outcome="ranked", reason="", **common))

    lv = {t.target_id: load_value(t.load) for t in eligible}

    def key(t: RankedTarget):
        v = lv[t.target_id]
        return (_BAND_ORDER.get(t.distance_band, 4),
                0 if v is not None else 1,      # an opinion outranks silence
                v if v is not None else 0.0,
                t.target_id)

    eligible.sort(key=key)
    out: List[RankedTarget] = []
    for i, t in enumerate(eligible):
        v = lv[t.target_id]
        why = f"band{t.distance_band}"
        if t.distance_ms is not None:
            why += f" ({t.distance_ms:.0f}ms)"
        if v is None:
            why += "; load: no opinion"
        else:
            bits = []
            if t.load.get("in_flight") is not None:
                bits.append(f"in_flight={t.load['in_flight']}")
            if t.load.get("pressure") is not None:
                bits.append(f"pressure={t.load['pressure']}")
            why += "; " + ", ".join(bits) if bits else f"; load={v:.2f}"
        out.append(RankedTarget(**{**t.__dict__,
                                   "rank": i + 1,
                                   "outcome": "chosen" if i == 0 else "ranked",
                                   "reason": why}))

    all_rows = out + rows
    chosen = out[0].target_id if out else None
    return {
        "kind": kind,
        "vantage": vantage,
        "generated_at": now,
        "ttl_s": ttl_s,
        "chosen": chosen,
        "reason": _summary(out, rows, kind, vantage),
        "targets": [t.to_wire() for t in out],
        "candidates": all_rows,
    }


def _summary(out: List[RankedTarget], filtered: List[RankedTarget],
             kind: str, vantage: str) -> str:
    if not out:
        return (f"no fresh, ready node hosts {kind} from {vantage}; "
                f"{len(filtered)} candidate(s) filtered")
    first = out[0]
    tail = ""
    if len(out) > 1:
        runner = out[1]
        tail = (f"; next was {runner.target_id} at band{runner.distance_band}"
                f" ({runner.reason})")
    return (f"{first.target_id} is the nearest fresh, ready {kind} from "
            f"{vantage} at band{first.distance_band}{tail}")


def is_stale(ranking: dict, now: Optional[float] = None) -> bool:
    """Past its TTL. A consumer must DISCARD rather than downgrade — a stale
    ranking is worse than none, because none falls back to a working default and
    stale looks authoritative."""
    now = time.time() if now is None else now
    return now - float(ranking.get("generated_at", 0)) > float(
        ranking.get("ttl_s", DEFAULT_TTL_S))
