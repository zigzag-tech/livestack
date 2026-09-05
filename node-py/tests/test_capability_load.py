"""The `load` block on /livestack/capability.

Why this exists: a consumer choosing between two engines has, until now, had no
way to tell an idle one from a saturated one. Latency is a lagging proxy — it
only reports saturation after somebody's request was the one that waited.

The contract these tests pin down is mostly about what the node must NOT say.
An absent or unmeasurable load report has to stay absent, so a consumer reads it
as "no opinion" and falls back to its own ranking. A fabricated zero would
advertise spare capacity we failed to establish, and would steer traffic at
precisely the node least able to serve it.
"""

import asyncio

import pytest

pytest.importorskip("livestack_node")
pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from fastapi import FastAPI  # noqa: E402
from livestack_node import ManagedUnit, ResidencyPolicy, attach, noop_free  # noqa: E402


def _mem(capacity, free):
    """The shape meters.py actually returns. Writing this out rather than a flat
    dict is the whole point of these tests: the first implementation assumed
    flat ints, int() on the nested dict raised, and the except swallowed it — so
    a perfectly working CUDA meter silently reported no pressure in production.
    """
    return {"capacity": {"vram_bytes": capacity}, "free": {"vram_bytes": free}}


def make_app(device_meter="absent", readiness=None):
    units = {
        "asr": ManagedUnit("asr", loader=lambda: "m", freer=noop_free,
                           residency_policy=ResidencyPolicy.HARD_PIN),
    }
    app = FastAPI()
    kwargs = {}
    if device_meter != "absent":
        kwargs["device_meter"] = device_meter
    manager, coord = attach(app, host_id="h", kind="polyasr", units=units,
                            idle_seconds=120, coload=True, gpu_call=lambda fn: fn(),
                            readiness=readiness, **kwargs)
    return app, manager


def client_for(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://test")


def cap(app):
    async def go():
        async with client_for(app) as c:
            r = await c.get("/livestack/capability")
            assert r.status_code == 200
            return r.json()
    return asyncio.run(go())


def test_load_reports_in_flight_and_pressure():
    app, _ = make_app(device_meter=lambda: _mem(100, 25))
    body = cap(app)
    load = body["load"]
    assert load["in_flight"] == 0
    assert load["device"] == {"capacity": 100, "free": 25}
    assert load["pressure"] == pytest.approx(0.75)
    assert "measured_at" in load


def test_a_real_lease_counts_as_in_flight():
    app, _ = make_app(device_meter=lambda: _mem(100, 100))

    async def go():
        async with client_for(app) as c:
            r = await c.post("/livestack/lease", json={"kind": "asr", "owner_id": "o"})
            assert r.status_code == 200
            return (await c.get("/livestack/capability")).json()

    assert asyncio.run(go())["load"]["in_flight"] == 1


def test_usage_leases_do_not_count_as_work():
    # The coordinator issues `__usage__:` leases to keep the idle-evict clock
    # alive. They mark recency, not work — counting them would leave a node that
    # served one request ten minutes ago looking permanently busy, which is the
    # exact misreport that would strand it.
    app, manager = make_app(device_meter=lambda: _mem(100, 100))
    manager.ensure("asr")          # takes a __usage__ lease, not a work lease
    assert cap(app)["load"]["in_flight"] == 0


def test_a_meter_that_throws_reports_no_pressure_rather_than_zero():
    def angry():
        raise RuntimeError("driver query failed")

    load = cap(make_app(device_meter=angry)[0])["load"]
    assert "pressure" not in load
    assert "device" not in load
    # It still reports what it CAN establish.
    assert load["in_flight"] == 0


def test_no_meter_means_no_pressure_claim():
    load = cap(make_app(device_meter=None)[0])["load"]
    assert "pressure" not in load
    assert load["in_flight"] == 0


def test_server_supplied_load_is_merged_not_replaced():
    # polyasr knows its own concurrent streams better than we can infer from
    # leases. Its answer wins on the keys it supplies, and the lease/meter facts
    # it does not supply survive.
    app, _ = make_app(device_meter=lambda: _mem(100, 40),
                      readiness=lambda: {"ready": True, "load": {"in_flight": 7}})
    load = cap(app)["load"]
    assert load["in_flight"] == 7            # server's count wins
    assert load["pressure"] == pytest.approx(0.6)   # ours survives


def test_cuda_meter_defaults_to_the_process_device_not_zero():
    """`cuda_meter()` used to hardcode device 0, which is right only on a
    single-GPU host. On a two-GPU box with one engine pinned to each card, both
    engines reported the SAME pressure — a load signal that is present,
    plausible, and useless, because two identical numbers cannot break a tie
    between the two nodes reporting them.
    """
    import types
    from livestack_node import meters

    seen = []

    fake = types.SimpleNamespace(
        cuda=types.SimpleNamespace(
            is_available=lambda: True,
            current_device=lambda: 1,
            mem_get_info=lambda d: (seen.append(d), (8, 16))[1],
        )
    )
    import sys
    prev = sys.modules.get("torch")
    sys.modules["torch"] = fake
    try:
        meters.cuda_meter()()
        assert seen == [1], f"metered {seen}, expected the process's own device"
        meters.cuda_meter(0)()
        assert seen == [1, 0], "an explicit index must still win"
    finally:
        if prev is None:
            del sys.modules["torch"]
        else:
            sys.modules["torch"] = prev
