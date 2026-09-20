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

import statistics
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
    # Where the node says it is. Carried, never ranked on: ordering is measured
    # distance, and region is operator policy the CALLER applies (see the
    # module header and `hostd.fleet_rank`). Carrying it is what lets the
    # caller apply that policy without a hardcoded host list of its own.
    region: Optional[str]
    state: str
    ready: bool
    distance_ms: Optional[float]
    distance_band: str
    load: Optional[Dict[str, Any]]
    inputs_at: Optional[float]
    outcome: str                       # chosen | ranked | filtered
    reason: str
    rank: Optional[int] = None
    # What the node says it HAS, beyond the kind it serves: a TTS server's
    # voice ids, an ASR's models. Distinct from `labels`, which are device
    # selectors matched one-to-one for a lease; this is an inventory, and its
    # values are usually lists. Carried for the same reason as `region` and
    # ranked on for the same reason as `region` — never. It is what lets a
    # caller state "a polytts that has THIS voice" in the request instead of
    # keeping a table of which host holds what, a table that is wrong the
    # first time somebody clones a voice.
    inventory: Optional[Dict[str, Any]] = None

    def to_wire(self) -> dict:
        return {"target_id": self.target_id, "node": self.node,
                "host_id": self.host_id, "device_id": self.device_id,
                "region": self.region, "inventory": self.inventory,
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
    * `region:<r>` — the MEDIAN of the measured link rows of the hosts in
      region `r` to the node's host. A caller off the tailnet has no links row
      of its own; rather than answer from the fleet broker's vantage (which is
      wherever the broker happens to sit), it borrows the region's own
      measurements — the same links a node inside that region would use.
      Unmeasured pairs contribute nothing; no row at all means no opinion.

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
    if scope == "region":
        name = name.lower()
        links: List[float] = []
        for host_id, host in (view.get("hosts") or {}).items():
            host = host or {}
            if _host_region(host) != name:
                continue
            ms = (host.get("links") or {}).get(node_host)
            if isinstance(ms, (int, float)):
                links.append(float(ms))
        if not links:
            return None
        # Median (statistics.median): one fast outlier host must not define a
        # region, and an even member count averages rather than picks the
        # larger.
        return statistics.median(links)
    return None


def _host_region(host: dict) -> Optional[str]:
    """A host's region, as its nodes declare it (the first that says).

    Hosts in the fleet view carry region on their NODE rows — one host, one
    region in practice, and a node that has not said is unknown, which a
    region vantage must not count as a member.
    """
    for node in host.get("nodes") or []:
        region = (node.get("region") or "").strip().lower()
        if region:
            return region
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
    # Fresh, serving this kind, and holding nothing yet. Used only when no warm
    # node exists: a cold node costs a load, and a load beats "no target".
    cold: List[RankedTarget] = []

    for host_id, host in sorted((view.get("hosts") or {}).items()):
        for node in host.get("nodes") or []:
            peer = node.get("peer", "")
            target_id = peer[: -len("/livestack")] if peer.endswith("/livestack") else peer
            dist = distance_to(view, host_id, node, vantage)
            band = distance_band(dist)
            load = node.get("load") if isinstance(node.get("load"), dict) else None
            common = dict(
                target_id=target_id, node=peer, host_id=host_id,
                region=node.get("region"),
                device_id=node.get("device_id"), state=node.get("state", "mia"),
                inventory=node.get("inventory") if isinstance(node.get("inventory"), dict) else None,
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
                # HELD BACK, NOT DISCARDED. For a node whose model loads on
                # demand, "no unit resident" means cold, not broken — it will
                # load when asked. Dropping it outright made the first request
                # after an idle eviction unroutable, so nothing could ever warm
                # it: measured on xc-tower-ubuntu 2026-09-18, a restarted
                # polytts left attune failing every item with `no polytts
                # target in na` while the node sat there, healthy and empty.
                cold.append(RankedTarget(
                    outcome="ranked",
                    reason=f"cold ({node.get('detail') or 'no unit resident'})",
                    **common))
                continue
            eligible.append(RankedTarget(outcome="ranked", reason="", **common))

    # Warm first, always. A cold node is a candidate only when there is no warm
    # one anywhere — preferring it otherwise would pay a model load to avoid a
    # few milliseconds of distance.
    if not eligible and cold:
        eligible = cold
        cold = []
    rows.extend(RankedTarget(**{**c.__dict__, "outcome": "filtered",
                                "reason": f"filtered: not ready ({c.reason})"})
                for c in cold)

    lv = {t.target_id: load_value(t.load) for t in eligible}

    def key(t: RankedTarget):
        v = lv[t.target_id]
        return (_BAND_ORDER.get(t.distance_band, 4),
                0 if v is not None else 1,      # an opinion outranks silence
                v if v is not None else 0.0,
                t.target_id)

    eligible.sort(key=key)
    # Which of the winners is cold, so the answer says the caller is paying for
    # a model load rather than presenting it as an ordinary placement.
    cold_ids = {t.target_id for t in eligible if t.reason.startswith("cold")}
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
        if t.target_id in cold_ids:
            why = f"cold, and the only {kind}; {why}"
        out.append(RankedTarget(**{**t.__dict__,
                                   "rank": i + 1,
                                   "outcome": "chosen" if i == 0 else "ranked",
                                   "reason": why}))

    all_rows = out + rows
    chosen = out[0].target_id if out else None
    return {
        "kind": kind,
        "vantage": vantage,
        # WHICH vantage answered. A caller that asked for region:na (or a
        # relay) gets the region's/relay's own measurements back — this field
        # is the receipt, so a ranking is never silently re-based onto the
        # fleet broker's own vantage by a path that ignored the ask.
        "vantage_used": vantage,
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
