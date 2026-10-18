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

A request routes across hosts when it states a SPEED preference, or when nothing
resident, unsaturated and in-policy on this node can serve it (`trigger` on the
route record): travelling then costs one hop, and staying costs a model load.

What a route is: a VIEW (the fleet broker's `/fleet`, cached, bounded stale) plus
a target that VALIDATES. `x-harmony-routed` carries the router's id, what it
expects (`expect=warm|cold`) and the contract version (`v=`). A warm-routed
request is served resident-only by the target or refused `409 route_stale`
before any byte; the router retries once (`send_routed`) and never after a
body byte has reached its caller. See openspec change harmony-regional-llm-routing.
"""
from __future__ import annotations

import copy
import json
import os
import threading
import time
from typing import Callable, NamedTuple, Optional

import httpx

from livestack_node.fleet_rank import rank

SPEED_METRICS = frozenset({"llm.decode_tok_s", "llm.first_token_ms"})
# Set on a request this routing already placed. The receiver must not route it
# again: two nodes with different views could otherwise bounce it.
ROUTED_HEADER = "x-harmony-routed"
# Version of the routed-request contract (the header's grammar and the
# `route_stale` refusal). A target that does not support the version a router
# sends answers 409 `route_contract` naming both, rather than misbehaving.
ROUTE_CONTRACT = 1
# The newest contract this build understands (a target's answer to `v=`).
SUPPORTED_CONTRACT = ROUTE_CONTRACT
_VIEW_TTL_S = 10.0
# How long the last good view may stand in when the fleet broker cannot be
# reached. Past it the node serves locally / same-host only: a view that old
# looks authoritative and is not.
STALE_MAX_S = float(os.environ.get("HARMONY_LLM_VIEW_STALE_MAX_S", "300"))
# The answer length assumed when a request sets no `max_tokens`: a typical
# chat reply. It only prices a cold host against a warm one (fleet_rank's
# finish-time rule); the request itself is forwarded unchanged.
DEFAULT_EXPECTED_TOKENS = 512

_lock = threading.Lock()
_cache: "dict[str, tuple[float, dict]]" = {}


class Routed(NamedTuple):
    router_id: str
    expect: str          # "warm" | "cold"
    version: int


def format_routed(router_id: str, expect: str, version: int = ROUTE_CONTRACT) -> str:
    return f"{router_id};expect={expect};v={version}"


def parse_routed(value: "str | None") -> "Routed | None":
    """`<router-id>;expect=warm|cold;v=<n>`. A bare id (the pre-contract form)
    is a cold route at version 1: a missing `expect` means cold, today's
    behaviour. None when the header is absent."""
    if not value or not value.strip():
        return None
    parts = [p.strip() for p in value.split(";")]
    fields = dict(p.split("=", 1) for p in parts[1:] if "=" in p)
    expect = fields.get("expect", "cold")
    try:
        version = int(fields.get("v", 1))
    except ValueError:
        version = -1
    return Routed(parts[0], expect if expect in ("warm", "cold") else "cold", version)


def new_request_id() -> str:
    import uuid
    return uuid.uuid4().hex


def wants_fleet_route(prefer, headers, *, no_warm_local: bool = False) -> "str | None":
    """Why this request should be ranked over the fleet, or None to serve as
    before: a speed clause in `prefer` ("speed"), or no resident, unsaturated,
    in-policy local unit can serve it ("no_warm_local"). Never for a request that
    was already routed -- two nodes with different views could bounce it."""
    if headers.get(ROUTED_HEADER):
        return None
    if any(isinstance(c, dict) and c.get("metric") in SPEED_METRICS
           for c in (prefer or [])):
        return "speed"
    return "no_warm_local" if no_warm_local else None


def fleet_view(url: str, timeout: float = 3.0, stale_max_s: "float | None" = None) -> "dict | None":
    """`GET <fleet broker>/fleet`, cached briefly. On a failed refresh the last
    good view stands in for up to `stale_max_s` (default STALE_MAX_S); past that
    None -- the caller keeps its local behaviour (a missing view is not a
    verdict)."""
    now = time.time()
    stale_max = STALE_MAX_S if stale_max_s is None else stale_max_s
    with _lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < _VIEW_TTL_S:
            return hit[1]
    try:
        view = httpx.get(f"{url.rstrip('/')}/fleet", timeout=timeout).json()
        if not isinstance(view, dict) or not isinstance(view.get("hosts"), dict):
            raise ValueError("not a fleet view")
    except Exception:
        if hit and now - hit[0] <= stale_max:
            return hit[1]
        return None
    with _lock:
        _cache[url] = (now, view)
    return view


def view_age_s(url: str) -> "float | None":
    """Seconds since the cached view for `url` was fetched."""
    with _lock:
        hit = _cache.get(url)
    return None if hit is None else round(time.time() - hit[0], 1)


def mark_cold(url: str, target_id: str) -> None:
    """A target said it holds nothing resident: drop it from the cached view as
    a warm candidate until the next refresh, so the next request does not go
    straight back to it."""
    with _lock:
        hit = _cache.get(url)
        if not hit:
            return
        view = copy.deepcopy(hit[1])
        for host in (view.get("hosts") or {}).values():
            for node in host.get("nodes") or []:
                peer = str(node.get("peer") or "")
                if (peer[: -len("/livestack")] if peer.endswith("/livestack") else peer) == target_id:
                    node["ready"] = False
                    for u in node.get("units") or []:
                        if isinstance(u, dict):
                            u["resident"] = False
        _cache[url] = (hit[0], view)


def node_vantage() -> str:
    """The caller's position as the ranker names it: `region:<LIVESTACK_NODE_REGION>`
    on a regional node or router, else the broker's own ("direct"). The ranking
    answers "closest to the ASKER", not to the fleet broker that was consulted."""
    region = os.environ.get("LIVESTACK_NODE_REGION", "").strip().lower()
    return f"region:{region}" if region else "direct"


def choose(view: dict, require: "dict | None", prefer: list, self_base: str,
           now: "float | None" = None,
           expected_tokens: "int | None" = None,
           vantage: "str | None" = None,
           regions: "list[str] | None" = None,
           exclude: "tuple | list" = (),
           kind: str = "llm") -> "tuple[Optional[str], dict]":
    """(base URL to forward to, or None to serve here; the routing record).

    The record is the explanation: who won, every ranked target with its
    reason and preference receipt, and every filtered one with the rule that
    removed it. It goes on the demand record and is what `/harmony/route`
    returns for a dry run. `expect` says whether the chosen target was warm in
    the ranking (the receiver then serves resident-only) or cold (it may load).
    `exclude` drops targets a first attempt already failed against."""
    tokens = expected_tokens or DEFAULT_EXPECTED_TOKENS
    vantage = vantage or node_vantage()
    if exclude:
        drop = {e.rstrip("/") for e in exclude}
        view = {**view, "hosts": {
            hid: {**h, "nodes": [n for n in h.get("nodes") or []
                                 if str(n.get("peer") or "").rsplit("/livestack", 1)[0].rstrip("/")
                                 not in drop]}
            for hid, h in (view.get("hosts") or {}).items()}}
    result = rank(view, kind, vantage=vantage, require=require or {"class": kind},
                  prefer=prefer, now=now, expected_tokens=tokens,
                  allow_regions=regions)
    chosen = result.get("chosen")
    targets = result.get("targets") or []
    top = targets[0] if targets else {}
    record = {
        "chosen": chosen,
        "self": self_base,
        "decision": ("no fleet target" if not chosen
                     else "serve here" if _same(chosen, self_base) else "forward"),
        "reason": result.get("reason"),
        "vantage": vantage,
        "regions": list(regions or []),
        "expect": ("cold" if str(top.get("reason") or "").startswith("cold") else "warm")
                  if chosen else None,
        "contract_version": ROUTE_CONTRACT,
        "expected_tokens": tokens,
        "view_generated_at": view.get("generated_at"),
        "targets": [{k: t.get(k) for k in ("target_id", "host_id", "rank", "reason",
                                           "preferences", "finish_s")}
                    for t in targets],
        "filtered": [{"target_id": c.target_id, "host_id": c.host_id, "reason": c.reason}
                     for c in result.get("candidates") or []
                     if getattr(c, "outcome", "") == "filtered"
                     and not c.reason.startswith("filtered: does not host")],
    }
    if not chosen or _same(chosen, self_base):
        return None, record
    return chosen, record


class RouteFailed(Exception):
    """Both attempts (or the only one) were refused; `status` is what the caller
    gets and `attempts` is everything that was tried."""

    def __init__(self, status: int, attempts: list):
        self.status, self.attempts = status, attempts
        super().__init__(json.dumps({"error": "route_failed", "attempts": attempts}))


async def send_routed(*, first: "tuple[str, str]", rechoose: "Callable[[list], tuple[Optional[str], str]]",
                      router_id: str, request_id: str, method: str, path: str, body: bytes,
                      headers: dict, params: dict, timeout, attempts: list,
                      on_stale: "Callable[[str], None] | None" = None):
    """Forward to `first = (target base, expect)`; on a refusal that costs no
    caller-visible byte -- `409 route_stale`, a connection failure before the
    response, or `429` -- ask `rechoose(excluded targets)` once and try the one
    target it names. Returns `(response, client)` of the attempt that answered,
    still unread, for the caller to stream. Raises RouteFailed with every attempt
    when nothing answered. A refusal is only ever acted on BEFORE the first body
    byte reaches the caller (nothing here streams), so a retry cannot replay a
    half-delivered answer.

    `attempts` is appended in place: `{target, expect, result}` per try."""
    tried: list = []
    target, expect = first
    for attempt in range(2):
        tried.append(target)
        hdrs = {**headers, ROUTED_HEADER: format_routed(router_id, expect),
                "x-request-id": request_id}
        client = httpx.AsyncClient(timeout=timeout)
        refused, status = None, 502
        try:
            req = client.build_request(method, f"{target}/v1/{path}", content=body,
                                       headers=hdrs, params=params)
            resp = await client.send(req, stream=True)
            if resp.status_code == 429:
                refused, status = "saturated (429)", 429
            elif resp.status_code == 409:
                raw = await resp.aread()
                try:
                    err = json.loads(raw)
                except Exception:
                    err = {}
                if err.get("error") == "route_stale":
                    refused, status = f"route_stale/{err.get('reason')}", 503
                    if err.get("reason") == "not_resident" and on_stale:
                        on_stale(target)
                else:
                    # Some other 409 (a contract mismatch) is the target's final
                    # answer, not a stale view: hand it back untouched.
                    attempts.append({"target": target, "expect": expect, "result": "409"})
                    return _Replay(resp.status_code, resp.headers, raw), client
            if refused is None:
                attempts.append({"target": target, "expect": expect, "result": "ok"})
                return resp, client
            await resp.aclose()
        except Exception as e:
            refused, status = f"{type(e).__name__}: {e or '(no message)'}", 502
        await client.aclose()
        attempts.append({"target": target, "expect": expect, "result": refused})
        if attempt == 1:
            break
        nxt, nxt_expect = rechoose(list(tried))
        if not nxt:
            break
        target, expect = nxt, nxt_expect
    raise RouteFailed(status, attempts)


class _Replay:
    """A read response replayed to a streaming caller (`aiter_raw`/`aclose`)."""

    def __init__(self, status_code, headers, raw):
        self.status_code, self.headers, self._raw = status_code, headers, raw

    async def aiter_raw(self):
        yield self._raw

    async def aread(self):
        return self._raw

    async def aclose(self):
        return None


def route_row(ctx: dict, router_id: str, outcome: str, status: int,
              now: "float | None" = None) -> dict:
    """The router's own record of a request it placed, for the ROUTE log (kept
    apart from the demand log: its readers count a record as a request served,
    and the target serves it). Joined to the target's demand record by
    `request_id`."""
    now = time.time() if now is None else now
    return {"ts": ctx["t0"], "role": "router", "router_id": router_id,
            "request_id": ctx.get("request_id"), "principal": ctx.get("principal"),
            "owner_ns": ctx.get("owner_ns"),
            "requirement_hash": ctx.get("requirement_hash"),
            "fleet_route": ctx.get("fleet_route"), "outcome": outcome,
            "http_status": status, "elapsed_ms": round((now - ctx["t0"]) * 1000, 1)}


def _same(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")
