"""A lease whose id contains slashes can still be heartbeated and released.

Fleet lease ids embed the hosted device id, which is a URL, so they carry
`/`. Clients percent-encode it; the server decodes before routing. These routes
must match anyway, or the lease is never handed back and lives out its TTL.
"""
import asyncio
from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from livestack_node.hostd import build_app  # noqa: E402


class _Broker:
    peers = []
    fleet_principals = None

    def __init__(self):
        self.released = []
        self.beaten = []

    def hosted_release(self, lease_id):
        self.released.append(lease_id)
        return True

    def hosted_heartbeat(self, lease_id):
        self.beaten.append(lease_id)
        return True


LEASE = "http://100.64.0.18:8190-1790149123237-1"


@pytest.mark.parametrize("encoded", [quote(LEASE, safe=""), LEASE])
def test_release_and_heartbeat_match_a_lease_id_with_slashes(encoded):
    broker = _Broker()

    async def exercise():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=build_app(broker)),
                base_url="http://testserver") as client:
            heartbeat = await client.post(f"/lease/{encoded}/heartbeat")
            release = await client.post(f"/lease/{encoded}/release")
            return heartbeat.json(), release.json()

    heartbeat, release = asyncio.run(exercise())
    assert heartbeat == {"ok": True}
    assert release == {"ok": True}

    assert broker.beaten == [LEASE]
    assert broker.released == [LEASE]


class _ExclusiveBroker(_Broker):
    def __init__(self):
        super().__init__()
        self.exclusive_beaten = []
        self.exclusive_released = []

    def exclusive_heartbeat(self, lease_id, ttl_s=120.0):
        self.exclusive_beaten.append((lease_id, ttl_s))
        return True

    def exclusive_release(self, lease_id):
        self.exclusive_released.append(lease_id)
        return True


def test_device_lease_routes_forward_ttl_and_do_not_use_hosted_lease_methods():
    broker = _ExclusiveBroker()
    lease_id = "device-lease-http-test"

    async def exercise():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=build_app(broker)),
                base_url="http://testserver") as client:
            custom = await client.post(f"/lease/{lease_id}/heartbeat",
                                       json={"ttl_seconds": 17})
            default = await client.post(f"/lease/{lease_id}/heartbeat")
            release = await client.post(f"/lease/{lease_id}/release")
            return custom, default, release

    custom, default, release = asyncio.run(exercise())
    assert custom.status_code == default.status_code == release.status_code == 200
    assert custom.json() == default.json() == release.json() == {"ok": True}
    assert broker.exclusive_beaten == [(lease_id, 17), (lease_id, 120.0)]
    assert broker.exclusive_released == [lease_id]
    assert broker.beaten == []
    assert broker.released == []


class _HoldBroker(_Broker):
    def __init__(self):
        super().__init__()
        self.holds = {}

    def set_device_hold(self, device_id, reason):
        if reason:
            self.holds[device_id] = reason
        else:
            self.holds.pop(device_id, None)

    @property
    def device_holds(self):
        return self.holds


@pytest.mark.parametrize("encoded", ["zz-tower0%2F2aef6b61", "zz-tower0/2aef6b61"])
def test_hold_route_matches_a_device_id_with_a_slash(encoded):
    broker = _HoldBroker()

    async def exercise():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=build_app(broker)),
                base_url="http://testserver") as client:
            held = await client.post(f"/devices/{encoded}/hold",
                                     json={"reason": "histo training"})
            freed = await client.post(f"/devices/{encoded}/hold", json={"reason": None})
            return held.status_code, held.json(), freed.json()

    status, held, freed = asyncio.run(exercise())
    assert status == 200
    assert held["holds"] == {"zz-tower0/2aef6b61": "histo training"}
    assert freed["holds"] == {}
