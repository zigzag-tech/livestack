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
