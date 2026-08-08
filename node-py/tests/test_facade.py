"""REST facade over attach()+polycore. Driven via httpx ASGITransport (starlette's
TestClient is incompatible with some httpx versions). Skipped without deps."""

import asyncio

import pytest

pytest.importorskip("livestack_node")
pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from livestack_node import ManagedUnit, ResidencyPolicy, noop_free  # noqa: E402

from livestack_node import attach  # noqa: E402


def make_app():
    units = {
        n: ManagedUnit(n, loader=(lambda n=n: f"m:{n}"), freer=noop_free,
                       residency_policy=(ResidencyPolicy.HARD_PIN if n == "asr"
                                         else ResidencyPolicy.UNPINNED))
        for n in ("asr", "diarize")
    }
    app = FastAPI()
    manager, coord = attach(app, host_id="h", kind="polyasr", units=units,
                            idle_seconds=120, coload=True, gpu_call=lambda fn: fn())
    return app, manager


def run(coro):
    return asyncio.run(coro)


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def test_lease_warms_and_releases():
    app, manager = make_app()

    async def scenario():
        async with client_for(app) as c:
            r = await c.post("/livestack/lease", json={"kind": "diarize", "owner_id": "o"})
            assert r.status_code == 200
            lease_id = r.json()["lease_id"]
            assert "diarize" in manager.resident          # warmed on acquire
            assert (await c.post(f"/livestack/lease/{lease_id}/heartbeat")).status_code == 200
            assert (await c.post(f"/livestack/lease/{lease_id}/release")).json()["released"] is True

    run(scenario())


def test_evict_pinned_409():
    app, manager = make_app()

    async def scenario():
        async with client_for(app) as c:
            await c.post("/livestack/model/warm", json={"unit": "asr"})
            r = await c.post("/livestack/model/evict", json={"unit": "asr"})
            assert r.status_code == 409

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


# -- readiness descriptor -----------------------------------------------------
#
# /capability is what a consumer reads INSTEAD of scraping /health, whose shape
# is each server's own business. `ready` must mean "fit to serve", never "a port
# answered" — that distinction is the whole reason the descriptor exists.

def make_ready_app(readiness):
    units = {"asr": ManagedUnit("asr", loader=lambda: "m", freer=noop_free,
                                residency_policy=ResidencyPolicy.HARD_PIN)}
    app = FastAPI()
    attach(app, host_id="h", kind="polyasr", units=units, idle_seconds=120,
           coload=True, gpu_call=lambda fn: fn(), readiness=readiness)
    return app


def test_capability_reports_not_ready_before_anything_is_resident():
    app = make_ready_app(None)
    async def go():
        async with client_for(app) as c:
            return (await c.get("/livestack/capability")).json()
    cap = run(go())
    assert cap["ready"] is False
    assert cap["kind"] == "polyasr"
    assert cap["units"] == ["asr"]


def test_the_servers_own_probe_decides_fitness():
    app = make_ready_app(lambda: {"ready": True, "detail": "streaming probe ok",
                                  "model": "Qwen3-ASR-1.7B", "concurrency": 3})
    async def go():
        async with client_for(app) as c:
            return (await c.get("/livestack/capability")).json()
    cap = run(go())
    assert cap["ready"] is True
    assert cap["model"] == "Qwen3-ASR-1.7B"
    assert cap["concurrency"] == 3


def test_a_readiness_probe_that_throws_is_not_ready():
    """Falling back to the generic answer would claim fitness we just failed to
    establish — the direction that sends audio to a broken engine."""
    def boom():
        raise RuntimeError("model handle is gone")
    app = make_ready_app(boom)
    async def go():
        async with client_for(app) as c:
            return (await c.get("/livestack/capability")).json()
    cap = run(go())
    assert cap["ready"] is False
    assert "model handle is gone" in cap["detail"]
