"""Exclusive-device admission through the real hostd ASGI application."""
import asyncio

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from livestack_node.hostbroker import HostBroker  # noqa: E402
from livestack_node.hostd import build_app  # noqa: E402
from livestack_node.planner import Device, Placement, Residency, Unit  # noqa: E402


class _Peer:
    def __init__(self, base, kind, *, resident=True):
        self.base = base
        self.host_id = "tower0"
        self.device_id = "gpu0"
        self.unit = Unit(kind, {"vram_bytes": 4_000}, priority=20,
                         residency=Residency.UNPINNED, min_residency_s=0)
        self._resident = resident
        self.calls = []

    def units(self):
        return {self.unit.kind: self.unit}

    def placements(self):
        return ([Placement(self.unit.kind, self.device_id, loaded_at=0.0)]
                if self._resident else [])

    def device_memory(self):
        return {"vram_bytes": 24_000}

    def device_capacity(self):
        return {"vram_bytes": 24_000}

    def warm(self, kind, device=None, budget=None):
        self.calls.append(("warm", kind))
        self._resident = True

    def evict(self, kind):
        self.calls.append(("evict", kind))
        self._resident = False

    def reclaim(self):
        self.calls.append(("reclaim",))
        return {"freed_bytes": 0}


def _broker(peers, clock):
    device = Device("gpu0", "tower0", capacity={"vram_bytes": 24_000},
                    reserved={"vram_bytes": 0})
    return HostBroker([device], peers, clock=clock)


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                             base_url="http://testserver")


def _exclusive_body(request_id="http-exclusive", owner="alice"):
    return {
        "id": request_id,
        "kind": "alpha",
        "owner": owner,
        "exclusive_device": True,
        "selector": {"device_id": "gpu0"},
        "min_free_bytes": 24_000,
        "wait_timeout_s": 5,
        "ttl_seconds": 30,
    }


def test_http_exclusive_lifecycle_drains_fences_heartbeats_and_releases(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LIVESTACK_LEDGER_DIR", str(tmp_path))
    now = [1000.0]
    alpha = _Peer("http://alpha", "alpha")
    beta = _Peer("http://beta", "beta")
    broker = _broker([alpha, beta], lambda: now[0])
    app = build_app(broker)

    async def exercise():
        async with _client(app) as client:
            acquired = await client.post("/admit", json=_exclusive_body())
            lease_id = acquired.json()["lease_id"]

            ordinary = await client.post("/admit", json={
                "id": "ordinary-other", "kind": "alpha", "owner": "bob"})
            wrong_owner = await client.post("/admit", json={
                "id": "wrong-owner", "kind": "alpha", "owner": "bob",
                "device_lease_id": lease_id,
                "selector": {"device_id": "gpu0"}})
            wrong_device = await client.post("/admit", json={
                "id": "wrong-device", "kind": "alpha", "owner": "alice",
                "device_lease_id": lease_id,
                "selector": {"device_id": "gpu1"}})
            owner = await client.post("/admit", json={
                "id": "owner-work", "kind": "alpha", "owner": "alice",
                "device_lease_id": lease_id})
            now[0] = 1010.0
            heartbeat = await client.post(
                f"/lease/{lease_id}/heartbeat", json={"ttl_seconds": 45})
            renewed_expiry = broker.exclusive_store.get_lease(lease_id)["expires_at"]
            now[0] = 1020.0
            released = await client.post(f"/lease/{lease_id}/release")
            repeated_release = await client.post(f"/lease/{lease_id}/release")
            after_release_heartbeat = await client.post(
                f"/lease/{lease_id}/heartbeat")
            shared = await client.post("/admit", json={
                "id": "shared-after", "kind": "beta", "owner": "bob"})
            return {
                "acquired": acquired.json(), "ordinary": ordinary.json(),
                "wrong_owner": wrong_owner.json(),
                "wrong_device": wrong_device.json(), "owner": owner.json(),
                "heartbeat": heartbeat.json(), "renewed_expiry": renewed_expiry,
                "released": released.json(),
                "repeated_release": repeated_release.json(),
                "after_release_heartbeat": after_release_heartbeat.json(),
                "shared": shared.json(), "lease_id": lease_id,
            }

    result = asyncio.run(exercise())
    acquired = result["acquired"]
    assert acquired["granted"] is True
    assert acquired["state"] == "exclusive"
    assert acquired["device_id"] == "gpu0"
    assert acquired["request_id"] == "http-exclusive"
    assert acquired["free_bytes"] == 24_000
    assert result["lease_id"].startswith("device-lease-")
    assert ("evict", "alpha") in alpha.calls
    assert ("evict", "beta") in beta.calls
    assert alpha.calls.count(("reclaim",)) == 1
    assert beta.calls.count(("reclaim",)) == 1

    for refused in (result["ordinary"], result["wrong_owner"],
                    result["wrong_device"]):
        assert refused["granted"] is False
        assert "degraded" not in refused
    assert result["owner"]["granted"] is True
    assert result["owner"]["device_id"] == "gpu0"
    assert ("warm", "alpha") in alpha.calls

    assert result["heartbeat"] == {"ok": True}
    assert result["renewed_expiry"] == 1055.0
    assert result["released"] == result["repeated_release"] == {"ok": True}
    assert result["after_release_heartbeat"] == {"ok": False}
    assert result["shared"]["granted"] is True
    assert result["shared"]["device_id"] == "gpu0"
    assert ("warm", "beta") in beta.calls
    assert (tmp_path / "device-exclusivity.sqlite3").is_file()


def test_http_build_app_reopens_persistent_exclusive_store(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVESTACK_LEDGER_DIR", str(tmp_path))
    now = [1000.0]
    first_broker = _broker(
        [_Peer("http://alpha-old", "alpha", resident=False)], lambda: now[0])
    first_app = build_app(first_broker)

    async def exercise():
        async with _client(first_app) as client:
            acquired = await client.post("/admit", json=_exclusive_body())
        lease_id = acquired.json()["lease_id"]

        second_broker = _broker(
            [_Peer("http://alpha-new", "alpha", resident=False)], lambda: now[0])
        second_app = build_app(second_broker)
        async with _client(second_app) as client:
            heartbeat = await client.post(
                f"/lease/{lease_id}/heartbeat", json={"ttl_seconds": 50})
            ordinary = await client.post("/admit", json={
                "id": "ordinary-after-reopen", "kind": "alpha", "owner": "bob"})
            owner = await client.post("/admit", json={
                "id": "owner-after-reopen", "kind": "alpha", "owner": "alice",
                "device_lease_id": lease_id,
                "selector": {"device_id": "gpu0"}})
            released = await client.post(f"/lease/{lease_id}/release")
            record = second_broker.exclusive_store.get_lease(lease_id)
            return (acquired.json(), heartbeat.json(), ordinary.json(),
                    owner.json(), released.json(), record, second_broker)

    (acquired, heartbeat, ordinary, owner, released, record,
     second_broker) = asyncio.run(exercise())
    assert acquired["granted"] is True
    assert heartbeat == {"ok": True}
    assert ordinary["granted"] is False and "degraded" not in ordinary
    assert owner["granted"] is True and owner["device_id"] == "gpu0"
    assert released == {"ok": True}
    assert record["state"] == "released"
    assert second_broker.exclusive_store.active("gpu0") == []


def test_http_multi_device_leases_validate_every_owner_and_place_only_primary(tmp_path, monkeypatch):
    monkeypatch.setenv("LIVESTACK_LEDGER_DIR", str(tmp_path))
    peer = _Peer("http://alpha", "alpha")
    meter = _Peer("http://gpu1-meter", "beta", resident=False)
    meter.device_id = "gpu1"
    broker = HostBroker([
        Device("gpu0", "tower0", capacity={"vram_bytes": 24_000}, reserved={"vram_bytes": 0}),
        Device("gpu1", "tower0", capacity={"vram_bytes": 24_000}, reserved={"vram_bytes": 0}),
    ], [peer, meter], clock=lambda: 1000.0)
    app = build_app(broker)

    async def exercise():
        async with _client(app) as client:
            first = await client.post("/admit", json=_exclusive_body("first"))
            second_body = _exclusive_body("second")
            second_body["selector"] = {"device_id": "gpu1"}
            second = await client.post("/admit", json=second_body)
            lease_ids = [first.json()["lease_id"], second.json()["lease_id"]]
            owner = await client.post("/admit", json={
                "id": "multi-owner", "kind": "alpha", "owner": "alice",
                "device_lease_ids": lease_ids})
            missing = await client.post("/admit", json={
                "id": "multi-missing", "kind": "alpha", "owner": "alice",
                "device_lease_ids": [lease_ids[0], "device-lease-missing"]})
            wrong_owner = await client.post("/admit", json={
                "id": "multi-wrong-owner", "kind": "alpha", "owner": "bob",
                "device_lease_ids": lease_ids})
            return first, second, owner, missing, wrong_owner

    first, second, owner, missing, wrong_owner = asyncio.run(exercise())
    assert first.json()["granted"] is second.json()["granted"] is True
    assert owner.json()["granted"] is True and owner.json()["device_id"] == "gpu0"
    assert missing.json()["granted"] is wrong_owner.json()["granted"] is False
    assert "degraded" not in owner.json()


def test_http_exclusive_input_errors_never_become_degraded_grants(
        tmp_path, monkeypatch):
    monkeypatch.setenv("LIVESTACK_LEDGER_DIR", str(tmp_path))
    broker = _broker([], lambda: 1000.0)
    app = build_app(broker)

    async def exercise():
        async with _client(app) as client:
            non_boolean = await client.post("/admit", json={
                "kind": "alpha", "exclusive_device": 1,
                "selector": {"device_id": "gpu0"}, "min_free_bytes": 1})
            missing_minimum = await client.post("/admit", json={
                "kind": "alpha", "exclusive_device": True,
                "selector": {"device_id": "gpu0"}})
            boolean_minimum = await client.post("/admit", json={
                "kind": "alpha", "exclusive_device": True,
                "selector": {"device_id": "gpu0"}, "min_free_bytes": True})
            missing_device = await client.post("/admit", json={
                "kind": "alpha", "exclusive_device": True,
                "min_free_bytes": 1})
            unknown_heartbeat = await client.post(
                "/lease/device-lease-missing/heartbeat")
            unknown_release = await client.post(
                "/lease/device-lease-missing/release")
            return (non_boolean, missing_minimum, boolean_minimum, missing_device,
                    unknown_heartbeat, unknown_release)

    responses = asyncio.run(exercise())
    assert [response.status_code for response in responses[:3]] == [400, 400, 400]
    missing_device = responses[3].json()
    assert missing_device["granted"] is False
    assert missing_device["exclusive_device"] is True
    assert "degraded" not in missing_device
    assert responses[4].json() == responses[5].json() == {"ok": False}
