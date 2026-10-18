"""Fleet routing for a request that states how fast it needs an answer.

A node's own path (resident unit here, a same-host peer that holds it, the host
broker's admission) never looks past this host: the fleet broker plans and
records, and the chat path did not ask it. So a caller that said
`harmony_prefer: [{"metric": "llm.decode_tok_s", "direction": "max"}]` got the
fastest unit ON THE HOST IT HAPPENED TO DIAL, which is a preference about the
caller's address book, not about speed.

This module answers the fleet question with the fleet's own ranking
(`livestack_node.fleet_rank.rank`) over the fleet broker's read-only view:
requirement first (what can serve it at all), warm before cold, then the
caller's preference over each node's MEASURED per-unit evidence (published by
`server._fleet_inventory`), then distance and load. The winner is a node URL;
the caller of `choose` forwards there with `ROUTED_HEADER` set so the receiving
node serves locally instead of asking again.

Only a SPEED preference routes across hosts. Without one, the existing local
behaviour stands exactly — an indifferent request has no basis to travel.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import httpx

from livestack_node.fleet_rank import rank

SPEED_METRICS = frozenset({"llm.decode_tok_s", "llm.first_token_ms"})
# Set on a request this routing already placed. The receiver must not route it
# again: two nodes with different views could otherwise bounce it.
ROUTED_HEADER = "x-harmony-routed"
_VIEW_TTL_S = 10.0

_lock = threading.Lock()
_cache: "dict[str, tuple[float, dict]]" = {}


def wants_fleet_route(prefer, headers) -> bool:
    """A speed clause in `prefer`, and not already routed."""
    if headers.get(ROUTED_HEADER):
        return False
    return any(isinstance(c, dict) and c.get("metric") in SPEED_METRICS
               for c in (prefer or []))


def fleet_view(url: str, timeout: float = 3.0) -> "dict | None":
    """`GET <fleet broker>/fleet`, cached briefly. None when unreachable — the
    caller then keeps its local behaviour (a missing view is not a verdict)."""
    now = time.time()
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < _VIEW_TTL_S:
            return hit[1]
    try:
        view = httpx.get(f"{url.rstrip('/')}/fleet", timeout=timeout).json()
    except Exception:
        return None
    if not isinstance(view, dict) or not isinstance(view.get("hosts"), dict):
        return None
    with _lock:
        _cache[url] = (now, view)
    return view


def choose(view: dict, require: "dict | None", prefer: list, self_base: str,
           now: "float | None" = None) -> "tuple[Optional[str], dict]":
    """(base URL to forward to, or None to serve here; the routing record).

    The record is the explanation: who won, every ranked target with its
    reason and preference receipt, and every filtered one with the rule that
    removed it. It goes on the demand record and is what `/harmony/route`
    returns for a dry run."""
    result = rank(view, "llm", require=require or {"class": "llm"}, prefer=prefer,
                  now=now)
    chosen = result.get("chosen")
    record = {
        "chosen": chosen,
        "self": self_base,
        "decision": ("no fleet target" if not chosen
                     else "serve here" if _same(chosen, self_base) else "forward"),
        "reason": result.get("reason"),
        "view_generated_at": view.get("generated_at"),
        "targets": [{k: t.get(k) for k in ("target_id", "host_id", "rank", "reason",
                                           "preferences")}
                    for t in result.get("targets") or []],
        "filtered": [{"target_id": c.target_id, "host_id": c.host_id, "reason": c.reason}
                     for c in result.get("candidates") or []
                     if getattr(c, "outcome", "") == "filtered"
                     and not c.reason.startswith("filtered: does not host")],
    }
    if not chosen or _same(chosen, self_base):
        return None, record
    return chosen, record


def _same(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")
