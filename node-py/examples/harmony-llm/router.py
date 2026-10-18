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
import socket
import subprocess
import sys
import time

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import fleet_route as fr  # noqa: E402
from request_language import derived_requirements, requirement_from  # noqa: E402
from livestack_node.demand_log import demand_log_from_env, requirement_hash  # noqa: E402
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


@app.get("/health")
def health():
    """Version, region and fleet URL: what a rollout checks. `units` is empty on
    purpose -- peers probing `/health` for a resident unit find nothing here."""
    return {"status": "ok", "role": "router", "router_id": ROUTER_ID,
            "revision": REVISION, "contract": fr.ROUTE_CONTRACT, "region": REGION or None,
            "fleet_url": FLEET_URL or None, "units": {}}


def _principal(request: Request):
    try:
        token = bearer_token(request.headers.get("authorization"))
        return principal_for(_PRINCIPALS, token) if _PRINCIPALS and token else None
    except Exception:
        return None


def _plan(path: str, body):
    """(requirement, prefer, kind, regions, regions_source) for a request."""
    requirement, prefer = None, []
    parsed = body if isinstance(body, dict) else {}
    if parsed:
        requirement = requirement_from(parsed)
        try:
            prefer = parse_preference_list(parsed.get("harmony_prefer"))
        except PreferenceError as e:
            raise HTTPException(status_code=400, detail=f"harmony: {e}")
    derived = derived_requirements(path, parsed)
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


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    ctx = {"t0": time.time(), "request_id":
           (request.headers.get("x-request-id") or "").strip() or fr.new_request_id()}
    raw = await request.body()
    try:
        return await _route(path, request, raw, ctx)
    except HTTPException as e:
        ROUTES.record(**fr.route_row(ctx, ROUTER_ID, "refused", e.status_code))
        raise


async def _route(path: str, request: Request, raw: bytes, ctx: dict):
    body = None
    if raw:
        try:
            body = json.loads(raw)
        except Exception:
            body = None
    requirement, prefer, kind = _plan(path, body)
    ctx["requirement_hash"] = requirement_hash(requirement)
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

    def rechoose(tried):
        tgt, r = fr.choose(view, requirement, prefer, SELF_BASE, kind=kind,
                           expected_tokens=tokens, regions=regions, exclude=tried)
        return tgt, (r.get("expect") or "cold")

    # Caller headers travel as they came: credentials and owner assertions are the
    # caller's, never replaced or supplemented by anything of ours.
    headers = {k: v for k, v in request.headers.items()
               if k.lower() not in {"host", "content-length", fr.ROUTED_HEADER}}
    timeout = httpx.Timeout(float(os.environ.get("HARMONY_LLM_PROXY_TIMEOUT", "300")))
    try:
        resp, client = await fr.send_routed(
            first=(target, rec.get("expect") or "cold"), rechoose=rechoose,
            router_id=ROUTER_ID, request_id=ctx["request_id"], method=request.method,
            path=path, body=raw, headers=headers, params=dict(request.query_params),
            timeout=timeout, attempts=attempts,
            on_stale=lambda t: fr.mark_cold(FLEET_URL, t))
    except fr.RouteFailed as e:
        raise HTTPException(status_code=e.status,
                            detail={"error": "route_failed", "attempts": e.attempts})

    async def relay():
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()
            ROUTES.record(**fr.route_row(
                ctx, ROUTER_ID, "ok" if resp.status_code < 400 else "refused", resp.status_code))

    return StreamingResponse(
        relay(), status_code=resp.status_code,
        headers={k: v for k, v in resp.headers.items()
                 if k.lower() not in {"content-length", "transfer-encoding"}})


if __name__ == "__main__":
    import uvicorn
    print(f"[harmony-router] {ROUTER_ID} rev={REVISION} region={REGION or '-'} "
          f"fleet={FLEET_URL or '(unset)'} on {BIND_HOST}:{PORT}", flush=True)
    uvicorn.run(app, host=BIND_HOST, port=PORT)
