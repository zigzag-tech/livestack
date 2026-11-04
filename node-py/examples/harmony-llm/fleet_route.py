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

from livestack_node import placement
from livestack_node.placement_default import problem_from_fleet

# Kinds whose units declare a `class` attribute, so "class=<kind>" is a real
# default requirement. Anything else (speech: polyasr, polytts) ranks on kind alone.
CLASS_KINDS = frozenset({"llm", "embed"})
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


def mark_busy(url: str, target_id: str) -> None:
    """A target refused because its device is held (503 + advice): drop it from
    the cached view until the next refresh, so the next request does not go
    straight back to it. Same mechanism as `mark_cold`; the view's own TTL (10 s)
    bounds how long a wrong mark can last."""
    mark_cold(url, target_id)


def busy_advice(status: int, headers, raw: bytes) -> "tuple[bool, str, float | None]":
    """(is_busy, reason, retry_after_s) for a refusal a speech node sent.

    Only a 503 qualifies. `is_busy` when the body says `device_busy`, a
    `Retry-After` is present, or the facade's refusal text ("did not grant this
    device") says the planner declined; a bare 503 is still `(False, ..)` and is
    retried as an outage, not a busy window. `retry_after_s` is clamped to 1..300
    so a hostile node cannot park a route."""
    if status != 503:
        return False, "", None
    status_word, detail = "", ""
    try:
        doc = json.loads(raw or b"{}")
        if isinstance(doc, dict):
            status_word = str(doc.get("status") or "")
            detail = str(doc.get("detail") or "")
    except Exception:
        pass
    ra = headers.get("retry-after") if headers is not None else None
    try:
        retry_after = max(1.0, min(300.0, float(str(ra).strip()))) if ra is not None else None
    except ValueError:
        retry_after = 30.0
    busy = (status_word == "device_busy" or ra is not None
            or "did not grant this device" in detail)
    return busy, (detail or status_word or "device busy")[:200], retry_after


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
    # `class` is an LLM/embed unit attribute. A speech node's units (`asr`,
    # `align`) declare none, so defaulting the clause there would filter every
    # speech node as "no unit satisfies class=polyasr".
    require = require or ({"class": kind} if kind in CLASS_KINDS else None)
    # Eligibility (hard) is stamped by the problem builder; ordering (soft) is the
    # configured ranker's, falling back to default-v1 (livestack_node.placement).
    problem, result = problem_from_fleet(view, kind, vantage=vantage, require=require,
                                         prefer=prefer, regions=regions, expected_tokens=tokens,
                                         exclude=exclude, now=now)
    decision = placement.run(problem)
    by_id = {t["target_id"]: t for t in result.get("targets") or []}
    targets = [{**by_id[r["id"]], "rank": i + 1} if r["id"] in by_id else {"target_id": r["id"], "rank": i + 1}
               for i, r in enumerate(decision["ranking"])]
    chosen = decision["chosen"]
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
        "placement": {"problem_id": decision["problem_id"], "ranker": decision["ranker"],
                      "fallback": decision["fallback"], "shadow": decision["shadow"],
                      "attempt_policy": decision["attempt_policy"]},
    }
    if not chosen or _same(chosen, self_base):
        return None, record
    return chosen, record


class RouteFailed(Exception):
    """Both attempts (or the only one) were refused; `status` is what the caller
    gets and `attempts` is everything that was tried."""

    def __init__(self, status: int, attempts: list, retry_after_s: "float | None" = None):
        self.status, self.attempts, self.retry_after_s = status, attempts, retry_after_s
        super().__init__(json.dumps({"error": "route_failed", "attempts": attempts}))


async def send_routed(*, first: "tuple[str, str]", rechoose: "Callable[[list], tuple[Optional[str], str]]",
                      router_id: str, request_id: str, method: str, path: str, body: bytes,
                      headers: dict, params: dict, timeout, attempts: list,
                      on_stale: "Callable[[str], None] | None" = None,
                      max_attempts: int = 2, retry_unavailable: bool = False,
                      on_busy: "Callable[[str], None] | None" = None):
    """Forward to `first = (target base, expect)`; on a refusal that costs no
    caller-visible byte -- `409 route_stale`, a connection failure before the
    response, or `429` -- ask `rechoose(excluded targets)` once and try the one
    target it names. Returns `(response, client)` of the attempt that answered,
    still unread, for the caller to stream. Raises RouteFailed with every attempt
    when nothing answered. A refusal is only ever acted on BEFORE the first body
    byte reaches the caller (nothing here streams), so a retry cannot replay a
    half-delivered answer.

    `max_attempts` bounds the tries (default 2). `retry_unavailable` (speech)
    also treats a pre-byte `503` as a refusal to fail over from: `busy: <reason>`
    when it carries busy advice (`busy_advice`), `unavailable` otherwise. A busy
    target is dropped from the cached view via `on_busy`. `RouteFailed.retry_after_s`
    is the shortest advertised busy window, for the caller's `Retry-After`.

    `attempts` is appended in place: `{target, expect, result}` per try."""
    tried: list = []
    retry_after_s: "float | None" = None
    target, expect = first
    for attempt in range(max_attempts):
        tried.append(target)
        hdrs = {**headers, ROUTED_HEADER: format_routed(router_id, expect),
                "x-request-id": request_id}
        client = httpx.AsyncClient(timeout=timeout)
        refused, status = None, 502
        try:
            req = client.build_request(method, f"{target}/v1/{path}", content=body,
                                       headers=hdrs, params=params)
            resp = await client.send(req, stream=True)
            # Queue admission refusal is final: do not turn a caller deadline
            # into a retry or a new model load on another host.
            if resp.status_code == 409:
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
            elif retry_unavailable and resp.status_code == 503:
                raw = await resp.aread()
                busy, why, ra = busy_advice(503, resp.headers, raw)
                refused, status = (f"busy: {why}" if busy else f"unavailable: {why}"), 503
                if busy and on_busy:
                    on_busy(target)
                if ra is not None:
                    retry_after_s = ra if retry_after_s is None else min(retry_after_s, ra)
            if refused is None:
                attempts.append({"target": target, "expect": expect, "result": "ok"})
                return resp, client
            await resp.aclose()
        except Exception as e:
            refused, status = f"{type(e).__name__}: {e or '(no message)'}", 502
        await client.aclose()
        attempts.append({"target": target, "expect": expect, "result": refused})
        if attempt == max_attempts - 1:
            break
        nxt, nxt_expect = rechoose(list(tried))
        if not nxt:
            break
        target, expect = nxt, nxt_expect
    # EVERY target refused only because it was BUSY (`saturated`, or a 429): the
    # request is fine and the fleet is merely full. A warm-routed request is
    # resident-only and never queues, so a burst would turn into 503s where the
    # node's own path used to queue it (FIFO, bounded, 429 past the bound). Hand
    # it, cold-marked, to the first target: that is today's behaviour exactly
    # (the target may queue), and it is the only attempt that waits.
    if attempts and all(a["result"].endswith("saturated") or a["result"].startswith("saturated")
                        for a in attempts):
        first_target = attempts[0]["target"]
        hdrs = {**headers, ROUTED_HEADER: format_routed(router_id, "cold"),
                "x-request-id": request_id}
        client = httpx.AsyncClient(timeout=timeout)
        try:
            req = client.build_request(method, f"{first_target}/v1/{path}", content=body,
                                       headers=hdrs, params=params)
            resp = await client.send(req, stream=True)
            attempts.append({"target": first_target, "expect": "cold",
                             "result": "ok" if resp.status_code < 400 else str(resp.status_code)})
            return resp, client
        except Exception as e:
            await client.aclose()
            attempts.append({"target": first_target, "expect": "cold",
                             "result": f"{type(e).__name__}: {e or '(no message)'}"})
            status = 502
    raise RouteFailed(status, attempts, retry_after_s)


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
