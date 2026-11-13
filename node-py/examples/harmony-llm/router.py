"""The localhost router: a facade with zero units.

Run it on every machine that SENDS inference requests and holds no GPU. Callers
point their `base_url` at `http://127.0.0.1:8188/v1`; the router ranks the fleet
(`fleet_route.choose`) from its own region's vantage and forwards the request
straight to the serving node, so a burst of requests never takes a detour via a
central node (and, cross-region, never crosses the ocean to ask where to go).

What it is not, by construction:

* **Not a fleet node.** It never calls `attach()`, announces, or registers, so
  the fleet view never lists it and it can never be ranked as a target.
* **Not a credential holder.** It forwards the caller's `Authorization` /
  `X-Harmony-Owner` headers unchanged and attaches no fleet token, even when
  HARMONY_LLM_FLEET_TOKEN is set in its environment. (It may READ the principal
  table to resolve a caller's region allowance; it is not a bearer of it.)
* **Not an authority.** It routes on a TTL'd view; the target validates
  (`409 route_stale`), and the router retries once (`fleet_route.send_routed`).

The request language is parsed by `request_language`, the same code the facade
runs, so a clause cannot mean one thing here and another at the target.

Run: `python router.py` (HARMONY_LLM_FLEET_URL required; LIVESTACK_NODE_REGION
names the vantage; HARMONY_ROUTER_HOST/PORT default 127.0.0.1:8188).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time

import httpx
from unit_queue import wait_budget
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from fastapi.exceptions import RequestValidationError

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fleet_route as fr  # noqa: E402
from livestack_node import placement  # noqa: E402
from fleet_embed import AmbiguousEmbedder, embed_nodes, pick as pick_embed  # noqa: E402
from request_language import derived_requirements, requirement_from  # noqa: E402
from livestack_node import request_log as _request_log  # noqa: E402
from livestack_node.demand_log import demand_log_from_env, owner_namespace, requirement_hash  # noqa: E402
from livestack_node.fleet_auth import (AuthError, bearer_token, effective_regions,  # noqa: E402
                                       principal_for, principals_from_env)
from livestack_node.preferences import (PreferenceError, parse_preference_list)  # noqa: E402

ROUTER_ID = os.environ.get("HARMONY_ROUTER_ID", "").strip() or f"router-{socket.gethostname()}"
FLEET_URL = os.environ.get("HARMONY_LLM_FLEET_URL", "").strip()
REGION = os.environ.get("LIVESTACK_NODE_REGION", "").strip().lower()
BIND_HOST = os.environ.get("HARMONY_ROUTER_HOST", "127.0.0.1")
PORT = int(os.environ.get("HARMONY_ROUTER_PORT", "8188"))
# No node of this process is ever a target: a base URL no ranked row can equal.
SELF_BASE = f"router://{ROUTER_ID}"
ROUTES = demand_log_from_env(f"{ROUTER_ID}-routes", log=lambda m: print(m, flush=True))

app = FastAPI(title="harmony-router", version="1.0.0")
# Optional: refresh the fleet view off the request path (see fleet_route.start_refresher).
VIEW_REFRESH_S = float(os.environ.get("HARMONY_ROUTER_VIEW_REFRESH_S", "0") or 0)

# --- the client-facing error contract (benchday-nodes A.6) ----------------------
# Whatever answers a phone, directly or through a relay, must not name the fleet:
# no engine id, host, device-busy reason, attempt list, `Server` or `X-Harmony-*`.
# Speech endpoints (/v1/audio/transcriptions, /ws/transcribe) are ALWAYS public;
# HARMONY_ROUTER_PUBLIC_ERRORS=1 makes every path public (the speech router sets
# it; the loopback LLM router leaves it off because its callers are operators).
# The detail is not lost: it goes, with the request id, to the router's own log
# (`router_public_error`) and to the route/decision records.
PUBLIC_ALL = os.environ.get("HARMONY_ROUTER_PUBLIC_ERRORS", "").strip().lower() in ("1", "true", "yes")
PUBLIC_VOCAB = {"unauthorized": 401, "rate_limited": 429, "too_large": 413,
                "bad_request": 400, "unavailable": 503}
# Response headers a client may receive from an upstream answer. Everything else
# (server, x-harmony-*, via, x-powered-by, ...) is dropped.
PUBLIC_HEADER_ALLOW = frozenset({"content-type", "content-encoding", "cache-control"})
PUBLIC_RETRY_DEFAULT_S = 30
_REQUEST_ID_OK = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def _request_id(raw) -> str:
    """The caller's X-Request-Id when it is a short token, else a fresh one: the
    id is echoed to the client and written to logs, so it is never free text."""
    raw = (raw or "").strip()
    return raw if _REQUEST_ID_OK.match(raw) else fr.new_request_id()


def _refusal_text(detail) -> str:
    """The server-side cause of a refusal: a route_failed's last attempt, else the
    detail text. Goes to the ROUTE log only (`cause`, `cause_code`), never to a client."""
    if isinstance(detail, dict):
        attempts = detail.get("attempts") or []
        return str(attempts[-1].get("result") if attempts else detail.get("error") or "")
    return str(detail or "")


def _vocab_code(status: int) -> str:
    if status in (401, 403):
        return "unauthorized"
    if status == 429:
        return "rate_limited"
    if status == 413:
        return "too_large"
    if 400 <= status < 500:
        return "bad_request"
    return "unavailable"


WARM_PATH = "/v1/audio/warm"


def _is_public(path: str) -> bool:
    return PUBLIC_ALL or path in ("/v1/audio/transcriptions", WARM_PATH)


def _log_public_error(request_id: str, surface: str, status: int, code: str, detail) -> None:
    """Rule 13: the cause stays diagnosable by request id, in the server-side log."""
    try:
        text = detail if isinstance(detail, str) else json.dumps(detail, default=str)
    except Exception:
        text = repr(detail)
    print(json.dumps({"event": "router_public_error", "router_id": ROUTER_ID,
                      "request_id": request_id, "surface": surface, "status": status,
                      "code": code, "detail": text[:2000]}), flush=True)


def _retry_after_s(headers) -> int:
    ra = (headers or {}).get("Retry-After") or (headers or {}).get("retry-after")
    try:
        return int(max(1, min(300, float(ra))))
    except (TypeError, ValueError):
        return PUBLIC_RETRY_DEFAULT_S


def _public_response(request_id: str, status: int, detail, headers=None, surface="http"):
    code = _vocab_code(status)
    out_status = PUBLIC_VOCAB[code]
    _log_public_error(request_id, surface, status, code, detail)
    out_headers = {"x-request-id": request_id}
    if code == "unavailable":
        out_headers["Retry-After"] = str(_retry_after_s(headers))
    return JSONResponse({"error": code, "request_id": request_id},
                        status_code=out_status, headers=out_headers)


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    if _is_public(request.url.path):
        rid = getattr(request.state, "request_id", None) or _request_id(request.headers.get("x-request-id"))
        return _public_response(rid, exc.status_code, exc.detail, exc.headers)
    from fastapi.exception_handlers import http_exception_handler
    return await http_exception_handler(request, exc)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    if _is_public(request.url.path):
        rid = getattr(request.state, "request_id", None) or _request_id(request.headers.get("x-request-id"))
        return _public_response(rid, 400, str(exc.errors())[:500])
    from fastapi.exception_handlers import request_validation_exception_handler
    return await request_validation_exception_handler(request, exc)


@app.exception_handler(Exception)
async def _unhandled_error(request: Request, exc: Exception):
    """A bug on a public path is still answered from the fixed vocabulary: the
    framework default is a bare text body and (in debug) a traceback."""
    rid = getattr(request.state, "request_id", None) or _request_id(request.headers.get("x-request-id"))
    if _is_public(request.url.path):
        return _public_response(rid, 500, f"{type(exc).__name__}: {exc}", surface="unhandled")
    return JSONResponse({"detail": "Internal Server Error"}, status_code=500)


@app.on_event("startup")
async def _start_view_refresher():
    if VIEW_REFRESH_S > 0 and FLEET_URL:
        fr.start_refresher(FLEET_URL, VIEW_REFRESH_S,
                           float(os.environ.get("HARMONY_ROUTER_VIEW_TIMEOUT_S", "10")))


def _revision() -> str:
    env = os.environ.get("HARMONY_ROUTER_REV", "").strip()
    if env:
        return env
    try:
        return subprocess.run(["git", "-C", os.path.dirname(os.path.realpath(__file__)),
                               "rev-parse", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


REVISION = _revision()
_PRINCIPALS = principals_from_env(log=lambda m: print(m, flush=True))


# When set (e.g. `polyasr`), /health answers "can this router place that kind NOW":
# 503 + the ranking's reason when no eligible node exists. A relay probes its
# upstream's /health to decide whether the target is up; a router that said `ok`
# with nothing behind it would advertise a dead door.
HEALTH_KIND = os.environ.get("HARMONY_ROUTER_HEALTH_KIND", "").strip()


@app.get("/health")
def health():
    """Version, region and fleet URL: what a rollout checks. `units` is empty on
    purpose -- peers probing `/health` for a resident unit find nothing here."""
    body = {"status": "ok", "role": "router", "router_id": ROUTER_ID,
            "revision": REVISION, "contract": fr.ROUTE_CONTRACT, "region": REGION or None,
            "fleet_url": FLEET_URL or None, "units": {}}
    if PUBLIC_ALL:
        # What a probe behind a public edge may read: up or not. The router id,
        # region and fleet URL name the fleet; the revision stays for rollout readback.
        body = {"status": "ok", "revision": REVISION}
    if not HEALTH_KIND:
        return body
    if not PUBLIC_ALL:
        body["health_kind"] = HEALTH_KIND
    view = fr.fleet_view(FLEET_URL) if FLEET_URL else None
    if view is None:
        if PUBLIC_ALL:
            _log_public_error("health", "health", 503, "unavailable", "no fleet view (unreachable or stale)")
            return JSONResponse({"error": "unavailable"}, status_code=503,
                                headers={"Retry-After": str(PUBLIC_RETRY_DEFAULT_S)})
        return JSONResponse({**body, "status": "no_fleet_view",
                             "detail": "no fleet view (unreachable or stale)"}, status_code=503)
    target, rec = fr.choose(view, None, [], SELF_BASE, kind=HEALTH_KIND)
    if not target:
        if PUBLIC_ALL:
            _log_public_error("health", "health", 503, "unavailable", str(rec.get("reason")))
            return JSONResponse({"error": "unavailable"}, status_code=503,
                                headers={"Retry-After": str(PUBLIC_RETRY_DEFAULT_S)})
        return JSONResponse({**body, "status": "no_provider", "detail": str(rec.get("reason"))[:300]},
                            status_code=503)
    if not PUBLIC_ALL:
        body["providers"] = len(rec.get("targets") or [])   # pool size names the fleet
    return body


def _principal(request: Request):
    try:
        token = bearer_token(request.headers.get("authorization"))
        return principal_for(_PRINCIPALS, token) if _PRINCIPALS and token else None
    except Exception:
        return None


def _plan(path: str, body):
    """(requirement, prefer, kind) for a request."""
    if path in SPEECH_PATHS:
        # A speech request's body is audio (multipart), so the kind comes from
        # the endpoint, exactly as `embeddings` derives `class=embed`. Nothing
        # else is asked of the caller: the fleet view already knows readiness,
        # busy windows, region distance and load.
        return None, [], SPEECH_PATHS[path]
    requirement, prefer = None, []
    parsed = body if isinstance(body, dict) else {}
    if parsed:
        requirement = requirement_from(parsed)
        try:
            prefer = parse_preference_list(parsed.get("harmony_prefer"))
        except PreferenceError as e:
            raise HTTPException(status_code=400, detail=f"harmony: {e}")
    derived = derived_requirements(path, parsed)
    if path == "classifier":
        model = str(parsed.get("model") or "").strip()
        if requirement is None and model and model != "local":
            requirement = {f"adapter.{model}": True}
        derived = {"class": "llm"}
    requirement = {**(requirement or {}), **derived} if (requirement or derived) else None
    kind = "embed" if (derived or {}).get("class") == "embed" else "llm"
    return requirement, prefer, kind


def _regions(request: Request, body):
    asked = body.get("harmony_regions") if isinstance(body, dict) else None
    try:
        return effective_regions(_principal(request), asked)
    except AuthError as e:
        raise HTTPException(status_code=e.status, detail=f"harmony: {e.detail}")


def _view():
    if not FLEET_URL:
        raise HTTPException(status_code=503, detail="harmony-router: HARMONY_LLM_FLEET_URL is not set")
    view = fr.fleet_view(FLEET_URL)
    if view is None:
        raise HTTPException(
            status_code=503,
            detail=f"harmony-router: no fleet view from {FLEET_URL} (unreachable, and the last "
                   f"good view is older than {fr.STALE_MAX_S:.0f}s)")
    return view


def _record(ctx: dict, view_url: str, rec: dict, trigger: str, regions_source: str) -> None:
    age = fr.view_age_s(view_url)
    rec.update(trigger=trigger, request_id=ctx["request_id"], regions_source=regions_source,
               view_age_s=age, view_stale=bool(age is not None and age > 10.0))
    ctx["fleet_route"] = rec


def _expected_tokens(body):
    for key in ("max_completion_tokens", "max_tokens"):
        v = body.get(key) if isinstance(body, dict) else None
        if isinstance(v, int) and not isinstance(v, bool) and v > 0:
            return v
    return None


@app.post("/harmony/route")
async def route_explain(request: Request):
    """DRY RUN: where this chat request would be sent, and why. Sends nothing."""
    if PUBLIC_ALL:      # it names engines and hosts: operator-only, never on a public router
        raise HTTPException(status_code=404, detail="route explain disabled on a public router")
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="harmony: body must be JSON")
    requirement, prefer, kind = _plan("chat/completions", body)
    regions, source = _regions(request, body)
    _tgt, rec = fr.choose(_view(), requirement, prefer, SELF_BASE, kind=kind,
                          expected_tokens=_expected_tokens(body), regions=regions)
    ctx = {"request_id": request.headers.get("x-request-id") or fr.new_request_id()}
    _record(ctx, FLEET_URL, rec, "router", source)
    return JSONResponse({"router": ROUTER_ID, "revision": REVISION, "requirement": requirement,
                         "prefer": prefer, "fleet": rec})


# --- warm hint: POST /v1/audio/warm ----------------------------------------------
# A cheap "a dictation is probably coming" signal the hub (or the relay) sends when a
# person opens the app / fetches the ASR manifest. The router asks the likely
# provider(s) to be ready by doing what a real session does: opening a short
# /ws/transcribe session on them (a `start` frame), which is request-driven
# admission -- Harmony's planner grants it, warms the provider, and the idle policy
# alone decides when it is released again. Nothing is pinned or preloaded, the
# router holds no fleet credential (the caller's own bearer / query is forwarded, as
# for a real session), and it never calls a node's control surface.
#
# The synthetic session lives only as long as the load: a node announces a cold
# admission with `warming` frames about once a second; when none has arrived for
# PREWARM_SETTLE_S (or PREWARM_MAX_S has passed) the session is closed and the
# provider's ordinary idle timer starts. Closing releases the hold at once, so the
# provider stays warm for exactly its normal idle window and no longer.
# Answer: always 202 {"status":"accepted"} for an authenticated caller, whatever
# was or was not started (a probe learns nothing about the fleet).
PREWARM_TARGETS = max(1, min(2, int(os.environ.get("HARMONY_ROUTER_PREWARM_TARGETS", "1") or 1)))
PREWARM_MAX_S = float(os.environ.get("HARMONY_ROUTER_PREWARM_MAX_S", "60"))
PREWARM_SETTLE_S = float(os.environ.get("HARMONY_ROUTER_PREWARM_SETTLE_S", "2.5"))
# A provider asked within this window (or still being asked) is not asked again.
PREWARM_COOLDOWN_S = float(os.environ.get("HARMONY_ROUTER_PREWARM_COOLDOWN_S", "30"))
PREWARM_MAX_INFLIGHT = int(os.environ.get("HARMONY_ROUTER_PREWARM_MAX_INFLIGHT", "4"))
_prewarm_last: "dict[str, float]" = {}      # target -> when it was last asked (bounded: see _prewarm_gate)
_prewarm_inflight: set = set()
_prewarm_tasks: set = set()


def _prewarm_gate(target: str) -> "str | None":
    """Why this target is not asked now, or None to go ahead. Also bounds the
    memory: entries older than the cooldown are dropped on every call."""
    now = time.time()
    for t in [t for t, at in _prewarm_last.items() if now - at > PREWARM_COOLDOWN_S]:
        del _prewarm_last[t]
    if target in _prewarm_inflight:
        return "in flight"
    if target in _prewarm_last:
        return "cooldown"
    if len(_prewarm_inflight) >= PREWARM_MAX_INFLIGHT:
        return "too many in flight"
    return None


def _prewarm_event(rid: str, target: str, outcome: str, t0: float, **extra) -> None:
    print(json.dumps({"event": "router_prewarm", "router_id": ROUTER_ID, "request_id": rid,
                      "target": target, "outcome": outcome,
                      "ms": round((time.time() - t0) * 1000, 1), **extra}), flush=True)


async def _prewarm_one(rid: str, target: str, query: str) -> None:
    import asyncio
    import websockets
    t0 = time.time()
    _prewarm_inflight.add(target)
    _prewarm_last[target] = t0
    up = None
    outcome, saw = "settled", 0
    try:
        url = target.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + WS_PATH
        up = await asyncio.wait_for(websockets.connect(url + (f"?{query}" if query else ""), max_size=None), 5)
        await up.send(json.dumps({"type": "start", "protocol": 1, "sessionId": f"warm-{rid[:32]}"}))
        while True:
            left = PREWARM_MAX_S - (time.time() - t0)
            if left <= 0:
                outcome = "max"
                break
            try:
                msg = await asyncio.wait_for(up.recv(), min(left, PREWARM_SETTLE_S))
            except asyncio.TimeoutError:
                break                       # quiet: admitted (or already warm)
            typ, _clean = _client_frame(msg)
            if typ == "warming":
                saw += 1
            elif typ == "error":
                outcome = "refused"
                _prewarm_last.pop(target, None)         # a refusal is not a reason to wait 30 s
                fr.mark_busy(FLEET_URL, target)
                _log_public_error(rid, "prewarm", 503, "unavailable", {"target": target, "frame": str(msg)[:300]})
                break
    except Exception as e:  # noqa: BLE001 - a hint never raises; the cause goes to the journal
        outcome = "failed"
        _prewarm_last.pop(target, None)
        _log_public_error(rid, "prewarm", 503, "unavailable", f"{type(e).__name__}: {e or '(no message)'}")
    finally:
        _prewarm_inflight.discard(target)
        if up is not None:
            try:
                await up.close()
            except Exception:
                pass
        _prewarm_event(rid, target, outcome, t0, warming_frames=saw)


@app.post(WARM_PATH)
async def audio_warm(request: Request):
    import asyncio
    rid = _request_id(request.headers.get("x-request-id"))
    request.state.request_id = rid
    # Authenticated: a configured principal table must recognise the caller; with
    # none configured (the speech router holds no table) a bearer or query
    # credential must at least be present -- the relay in front authenticates, and
    # the credential is forwarded to the node exactly as for a real session.
    if _PRINCIPALS:
        if _principal(request) is None:
            raise HTTPException(status_code=401, detail="harmony: warm hint needs a known principal")
    elif not (request.headers.get("authorization") or request.url.query):
        raise HTTPException(status_code=401, detail="harmony: warm hint needs a credential")
    regions, _src = _regions(request, None)
    view = _view()
    chosen: list = []
    for _ in range(PREWARM_TARGETS):
        target, _rec = fr.choose(view, None, WS_DEFAULT_PREFER, SELF_BASE, kind="polyasr",
                                 regions=regions, exclude=chosen)
        if not target:
            break
        chosen.append(target)
    started = []
    for target in chosen:
        why_not = _prewarm_gate(target)
        if why_not:
            _prewarm_event(rid, target, "skipped", time.time(), why=why_not)
            continue
        task = asyncio.create_task(_prewarm_one(rid, target, request.url.query))
        _prewarm_tasks.add(task)
        task.add_done_callback(_prewarm_tasks.discard)
        started.append(target)
    return JSONResponse({"status": "accepted", "request_id": rid}, status_code=202,
                        headers={"x-request-id": rid})


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    # PER-ACCOUNT ISOLATION. Nothing in this process is keyed by, or shared
    # between, accounts except the fleet's topology (the cached view): every
    # request carries its own caller's credential and owner assertion to the
    # target untouched, the route record is attributed to that caller, and no
    # response, body or identity is kept after the request.
    ctx = {"t0": time.time(), "request_id": _request_id(request.headers.get("x-request-id")),
           "owner_ns": owner_namespace(request.headers.get("x-harmony-owner"))}
    request.state.request_id = ctx["request_id"]
    try:
        ctx["principal"] = _request_log.principal_label(
            request.headers.get("authorization"), _PRINCIPALS)
    except Exception:
        ctx["principal"] = None
    raw = await request.body()
    try:
        return await _route(path, request, raw, ctx)
    except HTTPException as e:
        ROUTES.record(**fr.route_row(ctx, ROUTER_ID, "refused", e.status_code, cause=_refusal_text(e.detail)))
        raise


# What the router routes. Anything else under /v1 is a facade-specific endpoint
# that only some nodes serve: forwarding it to whichever node ranks first would
# be a coin toss, so it is refused and named. `/v1/classifier` is routable because
# its `model` names the ADAPTER it scores with (`jemm`), which is a requirement
# (`adapter.jemm`) the ranking can match against each node's units.
# Speech endpoints the router places across hosts: path -> the fleet node KIND
# that serves it. Batch ASR only for now; streaming ASR (`/ws/transcribe`) and
# TTS are not under /v1 and need a websocket / non-/v1 mount (design: openspec
# change harmony-speech-routing). `align` is polyasr's forced alignment (each word of
# the audio with its start and end, POST /v1/align, served by every polyasr backend):
# a caller that needs word timings states it by the endpoint, as transcription does.
SPEECH_PATHS = {"audio/transcriptions": "polyasr", "align": "polyasr"}
# Optional operator override of the placement attempt policy for speech (the
# policy itself comes from the ranker, clamped: placement.clamp_attempt_policy).
SPEECH_ATTEMPTS = int(os.environ.get("HARMONY_ROUTER_SPEECH_ATTEMPTS", "0"))
ROUTABLE = {"chat/completions", "completions", "embeddings", "classifier", *SPEECH_PATHS}


async def _route_embeddings(request: Request, raw: bytes, body, requirement, ctx: dict):
    """An embeddings need goes to the fleet embed node that meets it -- by the
    facade's own rule (fleet_embed.pick): an embedding space is a data contract,
    so a need met by embedders of DIFFERENT models is a 400, never a choice by
    distance. The body is restated in the node's own terms (its model id)."""
    view = _view()
    try:
        node = pick_embed(requirement or {"class": "embed"}, embed_nodes([view]))
    except AmbiguousEmbedder as e:
        raise HTTPException(status_code=400, detail=f"harmony: {e}")
    if not node:
        raise HTTPException(status_code=503,
                            detail=f"harmony-router: no embed node meets {requirement}")
    ctx["fleet_route"] = {"decision": "forward", "chosen": node["base"], "kind": "embed",
                          "model": node["model"], "request_id": ctx["request_id"],
                          "trigger": "router", "contract_version": fr.ROUTE_CONTRACT}
    out = {**(body if isinstance(body, dict) else {}), "model": node["model"]}
    out.pop("harmony_requires", None)
    out.pop("harmony_regions", None)
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in {"host", "content-length", fr.ROUTED_HEADER}}
    headers["x-request-id"] = ctx["request_id"]
    timeout = httpx.Timeout(float(os.environ.get("HARMONY_LLM_PROXY_TIMEOUT", "300")))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{node['base']}/v1/embeddings", json=out, headers=headers)
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"embed node {node['base']} failed: {type(e).__name__}: {e or '(no message)'}")
    ROUTES.record(**fr.route_row(ctx, ROUTER_ID, "ok" if resp.status_code < 400 else "refused",
                                 resp.status_code))
    from fastapi.responses import Response
    return Response(content=resp.content, status_code=resp.status_code,
                    media_type=resp.headers.get("content-type", "application/json"))


async def _route(path: str, request: Request, raw: bytes, ctx: dict):
    if path not in ROUTABLE:
        raise HTTPException(
            status_code=404,
            detail=f"harmony-router routes {', '.join(sorted(ROUTABLE))}; /v1/{path} is served "
                   f"by specific nodes, so call one directly")
    body = None
    if raw:
        try:
            body = json.loads(raw)
        except Exception:
            body = None
    try:
        budget = wait_budget(body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    requirement, prefer, kind = _plan(path, body)
    ctx["requirement_hash"] = requirement_hash(requirement)
    if kind == "embed":
        return await _route_embeddings(request, raw, body, requirement, ctx)
    regions, source = _regions(request, body)
    view = _view()
    tokens = _expected_tokens(body)
    target, rec = fr.choose(view, requirement, prefer, SELF_BASE, kind=kind,
                            expected_tokens=tokens, regions=regions)
    _record(ctx, FLEET_URL, rec, "router", source)
    if not target:
        raise HTTPException(status_code=503,
                            detail=f"harmony-router: no {kind} target"
                                   + (f" in {'/'.join(regions)}" if regions else "")
                                   + f": {rec.get('reason')}")
    attempts: list = []
    rec["attempts"] = attempts
    speech = path in SPEECH_PATHS

    def rechoose(tried):
        tgt, r = fr.choose(view, requirement, prefer, SELF_BASE, kind=kind,
                           expected_tokens=tokens, regions=regions, exclude=tried)
        return tgt, (r.get("expect") or "cold")

    # Caller headers travel as they came: credentials and owner assertions are the
    # caller's, never replaced or supplemented by anything of ours.
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in {"host", "content-length", fr.ROUTED_HEADER}}
    timeout = httpx.Timeout(budget + float(os.environ.get("HARMONY_LLM_PROXY_TIMEOUT", "300")))
    try:
        resp, client = await fr.send_routed(
            first=(target, rec.get("expect") or "cold"), rechoose=rechoose,
            router_id=ROUTER_ID, request_id=ctx["request_id"], method=request.method,
            path=path, body=raw, headers=headers, params=dict(request.query_params),
            timeout=timeout, attempts=attempts,
            on_stale=lambda t: fr.mark_cold(FLEET_URL, t),
            on_busy=lambda t: fr.mark_busy(FLEET_URL, t),
            max_attempts=((SPEECH_ATTEMPTS if speech and SPEECH_ATTEMPTS > 0 else None)
                          or rec["placement"]["attempt_policy"]["max_attempts"]),
            retry_unavailable=speech)
    except fr.RouteFailed as e:
        placement.record_outcome(rec["placement"]["problem_id"], target=None, success=False,
                                 cause=(e.attempts[-1]["result"] if e.attempts else "no attempt")[:200],
                                 attempts=e.attempts,
                                 final_ms=round((time.time() - ctx["t0"]) * 1000, 1))
        # Exhaustion names every try and, when a node advertised a busy window,
        # when to come back: the reason a caller shows, never a bare 503.
        raise HTTPException(
            status_code=e.status, detail={"error": "route_failed", "attempts": e.attempts},
            headers={"Retry-After": str(int(e.retry_after_s))} if e.retry_after_s else None)

    placement.record_outcome(
        rec["placement"]["problem_id"], target=(attempts[-1]["target"] if attempts else None),
        success=resp.status_code < 400, cause=None if resp.status_code < 400 else str(resp.status_code),
        first_byte_ms=round((time.time() - ctx["t0"]) * 1000, 1), attempts=attempts)

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            ROUTES.record(**fr.route_row(
                ctx, ROUTER_ID, "ok" if resp.status_code < 400 else "refused", resp.status_code))

    if _is_public("/v1/" + path):
        if resp.status_code >= 400:
            # An engine's own refusal (its body can name devices, models, hosts) is
            # logged and answered from the fixed vocabulary.
            detail = (await resp.aread())[:500].decode("utf-8", "replace")
            await resp.aclose()
            await client.aclose()
            # The cause is a bounded token (status only), never the engine's text.
            ROUTES.record(**fr.route_row(ctx, ROUTER_ID, "refused", resp.status_code,
                                         cause=f"engine_refused_{int(resp.status_code):03d}"))
            return _public_response(ctx["request_id"], resp.status_code, detail, dict(resp.headers))
        keep = {k: v for k, v in resp.headers.items() if k.lower() in PUBLIC_HEADER_ALLOW}
        keep["x-request-id"] = ctx["request_id"]
        return StreamingResponse(relay(), status_code=resp.status_code, headers=keep)

    return StreamingResponse(
        relay(), status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in {"content-length", "transfer-encoding"}})


# --- streaming ASR: /ws/transcribe ---------------------------------------------
# One declarative request (the socket's `start`), placed by Harmony. The router
# opens the upstream socket on the best ranked polyasr node and relays frames.
# A node answers `started` at once and admits (or refuses: error frame + close
# 1013) AFTER that, so "refused" is only knowable for a short while: the session
# is UNCOMMITTED until the first message that is not started/resumed/warming
# arrives (a partial or final). While uncommitted every client frame is also kept
# (bounded: WS_REPLAY_MAX_BYTES), and a refusal (error frame, close 1013, connect
# failure) re-places the session on the next provider and REPLAYS them; the new
# node's `started` is swallowed because the client already has one. After commit
# a drop belongs to the client's own resume protocol (recognizedThroughSeq), not
# to placement: the router closes the client with the upstream's code.
WS_PATH = "/ws/transcribe"
WS_REPLAY_MAX_BYTES = int(os.environ.get("HARMONY_ROUTER_WS_REPLAY_MAX_BYTES", str(4 * 1024 * 1024)))
# Native streaming is a PREFERENCE (owner decision 2026-11-04): a synthetic-stream
# engine (the Mac) stays eligible and wins on distance when it is the only/nearest.
WS_DEFAULT_PREFER = [{"attribute": "asr.streaming", "value": "native"}]
_CONTROL = ("started", "resumed", "warming")


# What a client may see of a node's frame, per type: the ASR wire protocol's own fields and
# nothing else, so an extra field (an engine/host/model/device name a node adds, now or later)
# never reaches a phone. Error frames are not listed: they are rebuilt in the fixed vocabulary.
_WS_CLIENT_FIELDS = {
    "started": ("type", "protocol", "sessionId", "ackSeq", "control"),
    "resumed": ("type", "protocol", "sessionId", "ackSeq", "control"),
    "warming": ("type", "protocol", "sessionId", "elapsedMs", "expectedMs"),
    "ack": ("type", "protocol", "sessionId", "ackSeq"),
    "partial": ("type", "sessionId", "partial", "text", "delta"),
    "final": ("type", "sessionId", "stopId", "text", "recognizedThroughSeq"),
    "done": ("type", "sessionId", "stopId"),
    "stopExpired": ("type", "sessionId", "stopId", "reason"),
}


def _client_frame(msg):
    """(type, scrubbed text) for a node frame; scrubbed is None when the frame must not reach the
    client (binary, not JSON, an unknown type). A frame with an `error` and no type (a node's
    bare exception text) is an error frame."""
    if isinstance(msg, bytes):
        return None, None
    try:
        d = json.loads(msg)
    except Exception:
        return None, None
    if not isinstance(d, dict):
        return None, None
    typ = d.get("type")
    if typ is None and "error" in d:
        return "error", None
    keep = _WS_CLIENT_FIELDS.get(typ)
    if keep is None:
        return typ, None
    return typ, json.dumps({k: d[k] for k in keep if k in d})


def _target_cold(view: dict, target: str) -> bool:
    """True when the fleet view says the speech node at `target` holds its `asr`
    unit non-resident, i.e. a session placed there pays a model load. The ranker
    cannot say so for speech: it judges warmth only for units that declare a
    `class` (LLMs), so `rec["expect"]` is "warm" for every speech target. Unknown
    (no such node or unit in the view) is not cold: absence of evidence is not a
    load."""
    for host in (view.get("hosts") or {}).values():
        for node in host.get("nodes") or []:
            peer = str(node.get("peer") or "")
            if (peer[: -len("/livestack")] if peer.endswith("/livestack") else peer).rstrip("/") != target.rstrip("/"):
                continue
            asr = [u for u in node.get("units") or [] if isinstance(u, dict) and u.get("kind") == "asr"]
            return bool(asr) and not any(u.get("resident") for u in asr)
    return False


def _warming_frame(session_id, t0: float, expected_ms=None) -> dict:
    """The router's own `warming` frame: the same shape a node sends (see
    _WS_CLIENT_FIELDS), sent as soon as the router knows the session may wait for
    a model load -- the chosen provider ranks cold, or a first provider refused and
    the session was re-placed. `elapsedMs` counts from the client's `start`."""
    frame = {"type": "warming", "protocol": 1}
    if isinstance(session_id, str) and session_id:
        frame["sessionId"] = session_id[:128]
    frame["elapsedMs"] = int((time.time() - t0) * 1000)
    if expected_ms:
        frame["expectedMs"] = int(expected_ms)
    return frame


def _session_id_of(first) -> "str | None":
    try:
        d = json.loads(first.get("text") or "")
        sid = d.get("sessionId") if isinstance(d, dict) else None
        return sid if isinstance(sid, str) else None
    except Exception:
        return None


@app.websocket(WS_PATH)
async def ws_transcribe(ws: WebSocket):
    import asyncio
    import websockets
    await ws.accept()
    t0 = time.time()
    ctx = {"t0": t0, "request_id": _request_id(ws.headers.get("x-request-id")),
           "owner_ns": owner_namespace(ws.headers.get("x-harmony-owner"))}
    inbox: asyncio.Queue = asyncio.Queue()

    async def client_reader():
        try:
            while True:
                m = await ws.receive()
                if m["type"] == "websocket.disconnect":
                    break
                inbox.put_nowait(m)
        except (WebSocketDisconnect, RuntimeError):
            pass
        inbox.put_nowait(None)

    reader = asyncio.create_task(client_reader())
    sent: list = []          # replayable client messages, in order (uncommitted only)
    replayable = True
    sent_bytes = 0
    committed = acked = False
    attempts: list = []
    tried: list = []
    t_first = t_partial = None
    pid = None
    reason = "no speech target"
    retry_after = 30
    up = None
    final_cause = None       # why this session ended unserved (ROUTE log only)
    warned = 0               # router-origin `warming` frames sent on this session
    owe_warming = None       # why a `warming` frame is owed once the client holds `started`

    async def send_warming(why):
        """One router-origin `warming` frame per placement that may wait for a load.
        The cause (`why`) stays in the journal; the client sees only the frame."""
        nonlocal warned, owe_warming
        owe_warming = None
        warned += 1
        print(json.dumps({"event": "router_warming", "router_id": ROUTER_ID,
                          "request_id": ctx["request_id"], "why": why, "n": warned,
                          "after_ms": round((time.time() - t0) * 1000, 1)}), flush=True)
        try:
            await ws.send_json(_warming_frame(_session_id_of(sent[0]) if sent else None, t0))
        except Exception:
            pass

    async def refuse(why, status=503):
        # The client gets the fixed vocabulary; `why` (engine ids, hosts, device
        # reasons) and the attempt list go to the log and the decision record.
        code = _vocab_code(status)
        _log_public_error(ctx["request_id"], "ws", status, code,
                          {"reason": why, "attempts": attempts})
        try:
            await ws.send_json({"type": "error", "error": code, "requestId": ctx["request_id"],
                                "retryAfterS": retry_after})
            await ws.close(code=1013, reason=code)
        except Exception:
            pass

    try:
        # The first client message must be start/resume: it is the request.
        first = await inbox.get()
        if first is None:
            return
        sent.append(first)
        regions, _src = _regions(ws, None)
        view = _view()
        max_attempts = SPEECH_ATTEMPTS if SPEECH_ATTEMPTS > 0 else None
        n = 0
        while True:
            target, rec = fr.choose(view, None, WS_DEFAULT_PREFER, SELF_BASE, kind="polyasr",
                                    regions=regions, exclude=tried)
            if pid is None:
                pid = rec["placement"]["problem_id"]
                max_attempts = max_attempts or rec["placement"]["attempt_policy"]["max_attempts"]
                ctx["fleet_route"] = rec
                rec["attempts"] = attempts
            if not target:
                reason = (attempts[-1]["result"] if attempts else f"no polyasr target: {rec.get('reason')}")
                break
            n += 1
            tried.append(target)
            # One operator-log line per placement: which engine serves this session.
            # Ids and counts only, no audio; lets "which engine heard this?" be a grep.
            top = (rec.get("targets") or [{}])[0]
            print(json.dumps({"event": "router_engine_chosen", "router_id": ROUTER_ID,
                              "request_id": ctx["request_id"],
                              "session_id": _session_id_of(first),
                              "target": target, "engine_id": top.get("target_id"),
                              "host_id": top.get("host_id"), "attempt": n}), flush=True)
            # A cold provider, or a re-placement after a refusal, may hold the client
            # past its no-partial watchdog: say so now, not when a node first speaks.
            why_cold = ("re-placed after a refusal" if n > 1
                        else "ranked cold" if rec.get("expect") == "cold" or _target_cold(view, target) else None)
            if why_cold:
                if acked:
                    await send_warming(why_cold)
                else:
                    owe_warming = why_cold      # sent the moment the client holds `started`
            url = target.replace("http://", "ws://", 1).replace("https://", "wss://", 1) + WS_PATH
            q = ws.url.query
            refused = None
            try:
                up = await asyncio.wait_for(websockets.connect(url + (f"?{q}" if q else ""), max_size=None), 5)
                for m in sent:
                    await (up.send(m["bytes"]) if m.get("bytes") is not None else up.send(m["text"]))
                t_client = asyncio.create_task(inbox.get())
                t_up = asyncio.create_task(up.recv())
                while refused is None:
                    done, _ = await asyncio.wait({t_client, t_up}, return_when=asyncio.FIRST_COMPLETED)
                    if t_client in done:
                        m = t_client.result()
                        if m is None:
                            t_up.cancel()
                            return
                        if not committed:
                            size = len(m.get("bytes") or b"") + len(m.get("text") or "")
                            sent_bytes += size
                            if sent_bytes > WS_REPLAY_MAX_BYTES:
                                replayable = False      # cannot replay: from here the session is final
                                committed = True
                                sent.clear()
                            else:
                                sent.append(m)
                        await (up.send(m["bytes"]) if m.get("bytes") is not None else up.send(m["text"]))
                        t_client = asyncio.create_task(inbox.get())
                    if t_up in done:
                        try:
                            msg = t_up.result()
                        except websockets.ConnectionClosed as e:
                            if committed:
                                up_code = e.rcvd.code if e.rcvd else 1011
                                if up_code not in (1000, 1001):
                                    _log_public_error(ctx["request_id"], "ws", 503, "unavailable",
                                                      f"upstream closed {up_code}: {(e.rcvd.reason if e.rcvd else '')[:100]}")
                                try:
                                    # normal closes pass; anything else is generic
                                    await ws.close(code=up_code if up_code in (1000, 1001) else 1011,
                                                   reason="" if up_code in (1000, 1001) else "unavailable")
                                except Exception:
                                    pass
                                final_cause = "upstream_closed_after_commit"
                                placement.record_outcome(pid, target=target, success=False, cause="upstream_closed_after_commit",
                                                         first_byte_ms=t_first, first_partial_ms=t_partial, attempts=attempts)
                                t_client.cancel()
                                return
                            refused = f"closed {e.rcvd.code if e.rcvd else '?'}: {(e.rcvd.reason if e.rcvd else '') or 'before admission'}"
                            break
                        if t_first is None:
                            t_first = round((time.time() - t0) * 1000, 1)
                        typ, clean = _client_frame(msg)
                        if not committed and typ == "error":
                            try:
                                refused = json.loads(msg).get("error") or "error"
                            except Exception:
                                refused = "error"
                            break
                        if committed and typ == "error":
                            # a node's own error frame after admission: same vocabulary
                            _log_public_error(ctx["request_id"], "ws", 503, "unavailable", msg)
                            await ws.send_json({"type": "error", "error": "unavailable",
                                                "requestId": ctx["request_id"], "retryAfterS": retry_after})
                        elif clean is None:
                            # not part of the client protocol: never forwarded, never commits
                            _log_public_error(ctx["request_id"], "ws", 503, "unavailable",
                                              f"dropped node frame type={typ!r}")
                        elif not committed and typ in _CONTROL:
                            if typ == "warming" or not acked:
                                await ws.send_text(clean)
                            acked = acked or typ in ("started", "resumed")
                            if owe_warming and acked:
                                await send_warming(owe_warming)
                        else:
                            if not committed:
                                committed = True        # an ack / partial means the node admitted and is working
                                sent.clear()
                            if t_partial is None and typ in ("partial", "final"):
                                t_partial = round((time.time() - t0) * 1000, 1)
                            await ws.send_text(clean)
                        t_up = asyncio.create_task(up.recv())
                t_client.cancel()
                t_up.cancel()
            except WebSocketDisconnect:
                return
            except Exception as e:  # noqa: BLE001 - connect failure etc: a refusal, not a crash
                refused = f"{type(e).__name__}: {e or '(no message)'}"
            if up is not None:
                try:
                    await up.close()
                except Exception:
                    pass
                up = None
            attempts.append({"target": target, "expect": "cold", "result": f"refused: {str(refused)[:160]}"})
            reason = str(refused)
            if any(w in reason.lower() for w in ("did not grant", "busy", "held")):
                fr.mark_busy(FLEET_URL, target)
            if committed and not replayable:
                break
            if n >= max_attempts:
                break
        final_cause = reason
        await refuse(reason)
        placement.record_outcome(pid, target=None, success=False, cause=reason[:200], attempts=attempts,
                                 final_ms=round((time.time() - t0) * 1000, 1))
    except HTTPException as e:
        final_cause = str(e.detail)
        await refuse(str(e.detail), e.status_code)
    finally:
        # One ROUTE row per session, keyed by the request id the client was handed,
        # so the real cause of a refusal is joinable server-side (never sent to a client).
        ROUTES.record(**fr.route_row(ctx, ROUTER_ID, "refused" if final_cause else "ok" if committed else "abandoned",
                                     503 if final_cause else 101, cause=final_cause))
        reader.cancel()
        if up is not None:
            try:
                await up.close()
            except Exception:
                pass
        if committed and pid and attempts is not None and not any(a["result"] == "ok" for a in attempts):
            placement.record_outcome(pid, target=tried[-1] if tried else None, success=True,
                                     first_byte_ms=t_first, first_partial_ms=t_partial,
                                     final_ms=round((time.time() - t0) * 1000, 1), attempts=attempts)


def bind_hosts(value: str) -> list[str]:
    """`HARMONY_ROUTER_HOST` is one address or a comma list (loopback for a same-host relay plus
    the tailnet address for a relay on another host). Name each address: a wildcard would also
    answer on a public interface, and the router holds no credential."""
    hosts = list(dict.fromkeys(h.strip() for h in value.split(",") if h.strip()))
    if not hosts:
        raise ValueError("HARMONY_ROUTER_HOST names no address")
    return hosts


def bind_sockets(hosts: list[str], port: int) -> list[socket.socket]:
    socks = []
    try:
        for host in hosts:
            s = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM)
            socks.append(s)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except Exception:
        for s in socks:
            s.close()
        raise
    return socks


if __name__ == "__main__":
    import uvicorn
    print(f"[harmony-router] {ROUTER_ID} rev={REVISION} region={REGION or '-'} "
          f"fleet={FLEET_URL or '(unset)'} on {BIND_HOST}:{PORT}", flush=True)
    _hosts = bind_hosts(BIND_HOST)
    if len(_hosts) == 1:
        uvicorn.run(app, host=_hosts[0], port=PORT, server_header=False)
    else:
        uvicorn.Server(uvicorn.Config(app, server_header=False)).run(sockets=bind_sockets(_hosts, PORT))
