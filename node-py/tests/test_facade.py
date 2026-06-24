"""REST facade conformance (port 2).

Driven via httpx ASGITransport rather than starlette's TestClient, which is
incompatible across some starlette/httpx version pairs. Skipped if fastapi/httpx
are unavailable.
"""

import asyncio

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402

from livestack_node.facade import build_router  # noqa: E402
from livestack_node.lease import Capability  # noqa: E402
from livestack_node.residence import ResidenceController  # noqa: E402
from livestack_node.runtime import FakeRuntime  # noqa: E402


def make_app():
    runtime = FakeRuntime()
    units = {
        "asr": Capability(kind="asr", host_id="h", lease_ttl_seconds=100),
        "diarize": Capability(kind="diarize", host_id="h", lease_ttl_seconds=100),
    }
    ctrl = ResidenceController("h", runtime, units)
    cap = Capability(kind="polyasr", host_id="h", labels={"device": "cuda:0"})
    app = FastAPI()
    app.include_router(build_router(ctrl, cap), prefix="/livestack")
    return app, runtime


def run(coro):
    return asyncio.run(coro)


def client_for(app):
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


def test_lease_lifecycle_over_http():
    app, runtime = make_app()

    async def scenario():
        async with client_for(app) as c:
            r = await c.post("/livestack/lease", json={"kind": "diarize", "owner_id": "o"})
            assert r.status_code == 200
            lease_id = r.json()["lease_id"]
            assert runtime.resident() == ["diarize"]  # warm-on-acquire
            assert (await c.post(f"/livestack/lease/{lease_id}/heartbeat")).status_code == 200
            rel = await c.post(f"/livestack/lease/{lease_id}/release")
            assert rel.json()["released"] is True
            assert runtime.resident() == []  # released -> evicted

    run(scenario())


def test_pin_blocks_evict():
    app, runtime = make_app()

    async def scenario():
        async with client_for(app) as c:
            await c.post("/livestack/model/pin", json={"unit": "asr"})
            assert runtime.resident() == ["asr"]
            r = await c.post("/livestack/model/evict", json={"unit": "asr"})
            assert r.status_code == 409  # pinned units can't be evicted

    run(scenario())


def test_capacity_conflict_returns_409():
    app, _ = make_app()

    async def scenario():
        async with client_for(app) as c:
            assert (await c.post("/livestack/lease", json={"kind": "asr"})).status_code == 200
            assert (await c.post("/livestack/lease", json={"kind": "asr"})).status_code == 409

    run(scenario())


def test_health_and_capability():
    app, _ = make_app()

    async def scenario():
        async with client_for(app) as c:
            assert (await c.get("/livestack/health")).json()["status"] == "ok"
            cap = (await c.get("/livestack/capability")).json()
            assert cap["kind"] == "polyasr"
            assert set(cap["units"]) == {"asr", "diarize"}

    run(scenario())
