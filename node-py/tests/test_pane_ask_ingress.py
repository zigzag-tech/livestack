"""Authenticated facade with actual bounded HTTP engine transport.

The broker callback is instrumented: broker outage/owner forwarding cannot be
tested by the engine HTTP controls alone. Registry values are fixture controls,
not model qualification or deployment evidence.
"""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import time

from fastapi import FastAPI
import httpx
import pytest

from livestack_node.decisions.admission import Admission
from livestack_node.decisions.nominal_27b_adapter import Nominal27BExtractionAdapter
from livestack_node.decisions.pane_ask_ingress import PaneAskRuntime, install_pane_ask_route
from livestack_node.decisions.resident_extraction_scope import ResidentExtractionScope
from unit_queue import Queues
from test_decision_service import PRINCIPALS, _headers
from test_nominal_27b_adapter import engine_http, request


def scenario(engine, state, *, fault=None):
    async def run():
        app = FastAPI()
        install_pane_ask_route(app, lambda: PRINCIPALS)
        admissions, releases = [], []
        # Instrument native-manager metadata only. The actual UnitQueue and
        # HTTP engine are used; this does not certify native residency/eviction.
        counters = {"busy": 0, "touches": 0}
        unit = SimpleNamespace(busy=False)
        queues = Queues()

        def acquire():
            counters["busy"] += 1

        def release():
            counters["busy"] -= 1

        def touch():
            if fault == "guard_failure":
                raise RuntimeError("instrumented manager touch failure")
            counters["touches"] += 1

        manager = SimpleNamespace(units={engine.unit: unit}, resident={engine.unit}, touch=touch,
            coordinator=SimpleNamespace(report_busy=lambda *args: None))
        app.state.pane_ask_execution_scope = ResidentExtractionScope(manager=manager, queues=queues,
            busy=SimpleNamespace(acquire=acquire, release=release), max_concurrent=lambda name: 1,
            engine_is_up=lambda name: True, device_id=engine.device_id)

        def observe():
            state["protected"] = (unit.busy, counters["busy"], queues.status(engine.unit, 1)["in_flight"])

        state["before_generate"] = observe
        if fault == "no_scope":
            del app.state.pane_ask_execution_scope
        elif fault == "cold":
            manager.resident.clear()
        held = queues.try_acquire(engine.unit, 1) if fault == "saturated" else None

        async def admit(**scope):
            admissions.append(scope)
            if fault == "broker_down":
                raise ConnectionError("fixture broker unreachable")
            lease = "lease-test" if len(admissions) == 1 else "lease-retry"
            return Admission(True, engine.unit, engine.backend, engine.device_id, True, lease)

        async def finish(grant):
            releases.append(grant.lease_id)

        async with httpx.AsyncClient() as engine_client:
            selected = replace(engine, qualification_hash="unqualified") if fault == "unqualified" else engine
            if fault != "no_runtime":
                app.state.pane_ask_runtime = PaneAskRuntime(
                    Nominal27BExtractionAdapter(selected, client=engine_client), admit, finish)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://fixture") as client:
                headers = _headers(realm=None) if fault == "no_realm" else _headers()
                req = request()
                if fault in ("expired_running", "cancelled_running"):
                    if fault == "expired_running":
                        req["deadline_at_ms"] = int(time.time() * 1000) + 200
                    pending = asyncio.create_task(client.post("/v1/decisions", json=req, headers=headers))
                    until = time.monotonic() + 1
                    while not state["calls"] and time.monotonic() < until:
                        await asyncio.sleep(0.005)
                    assert state["calls"]
                    if fault == "cancelled_running":
                        pending.cancel()
                    await asyncio.sleep(0.25)
                    assert not state["completed"].is_set()
                    assert not pending.done()
                    assert releases == [] and unit.busy and counters["busy"] == 1
                    assert queues.status(engine.unit, 1)["in_flight"] == 1
                    if fault == "cancelled_running":
                        with pytest.raises(asyncio.CancelledError):
                            await pending
                        assert state["completed"].is_set()
                        assert releases == ["lease-test"]
                        assert not unit.busy and counters["busy"] == 0
                        assert queues.status(engine.unit, 1)["in_flight"] == 0
                        return None, admissions, releases, req
                    response = await pending
                else:
                    response = await client.post("/v1/decisions", json=req, headers=headers)
                if held:
                    held.release()
                if state.get("disconnect"):
                    assert counters["busy"] == 1 and unit.busy
                    assert queues.status(engine.unit, 1)["in_flight"] == 1
                    assert releases == []
                    # Another request must refuse without redispatching GPU work.
                    retry = await client.post("/v1/decisions", json=request(), headers=headers)
                    assert retry.status_code == 429
                    assert len(state["calls"]) == 1
                    assert releases == ["lease-retry"]  # only the undispatched retry
                    return response, admissions, releases, req
                assert counters["busy"] == 0
                assert queues.status(engine.unit, 1)["in_flight"] == 0
                assert not unit.busy
                return response, admissions, releases, req
    return asyncio.run(run())


def test_facade_forwards_authenticated_scope_and_closes_admission(engine_http):
    engine, state = engine_http
    response, admissions, releases, req = scenario(engine, state)
    assert response.status_code == 200
    assert response.json()["request_id"] == req["request_id"]
    assert len(state["calls"]) == 1
    assert state["protected"] == (True, 1, 1)
    assert admissions[0]["owner"] == "acct_dev" and admissions[0]["realm"] == "realm_dev"
    assert admissions[0]["request_id"] == req["request_id"]
    assert releases == ["lease-test"]


@pytest.mark.parametrize("fault,cause,status", [("broker_down", "broker_unavailable", 503),
    ("unqualified", "profile_unavailable", 503), ("no_runtime", "profile_unavailable", 503),
    ("no_realm", "forbidden", 403)])
def test_facade_refuses_before_warm_engine_inference(engine_http, fault, cause, status):
    engine, state = engine_http
    response, admissions, releases, _ = scenario(engine, state, fault=fault)
    assert response.status_code == status and response.json()["cause"] == cause
    assert state["calls"] == [] and releases == []
    assert len(admissions) == (1 if fault == "broker_down" else 0)


def test_facade_closes_admission_when_engine_output_fails(engine_http):
    engine, state = engine_http
    state["raw"] = b"invalid JSON"
    response, admissions, releases, _ = scenario(engine, state)
    assert response.status_code == 422 and response.json()["cause"] == "invalid_output"
    assert len(admissions) == 1 and releases == ["lease-test"]


def test_disconnected_engine_retains_reservations_and_refuses_redispatch(engine_http):
    engine, state = engine_http
    state["disconnect"] = True
    response, admissions, releases, _ = scenario(engine, state)
    assert response.status_code == 503 and response.json()["cause"] == "unhealthy"
    assert "reservations retained" in response.json()["detail"]
    assert len(admissions) == 2
    assert not state["completed"].is_set()


@pytest.mark.parametrize("fault,cause,status", [("no_scope", "profile_unavailable", 503),
    ("cold", "profile_unavailable", 503), ("saturated", "capacity", 429),
    ("guard_failure", "unhealthy", 503)])
def test_facade_refuses_unprotected_cold_or_saturated_execution(engine_http, fault, cause, status):
    engine, state = engine_http
    response, admissions, releases, _ = scenario(engine, state, fault=fault)
    assert response.status_code == status and response.json()["cause"] == cause
    assert state["calls"] == []
    expected = 0 if fault == "no_scope" else 1
    assert len(admissions) == expected
    assert len(releases) == expected


@pytest.mark.parametrize("fault", ["expired_running", "cancelled_running"])
def test_running_execution_keeps_all_reservations_until_backend_completion(engine_http, fault):
    engine, state = engine_http
    state["delay"] = 0.75
    response, admissions, releases, _ = scenario(engine, state, fault=fault)
    if response is not None:
        assert response.status_code == 504 and response.json()["cause"] == "deadline_exceeded"
    assert len(admissions) == 1 and releases == ["lease-test"]
    assert state["completed"].is_set()
