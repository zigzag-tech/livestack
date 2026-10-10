"""The status page projects bounded broker counters and already-read peer queues."""
import asyncio

import pytest

pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")
from fastapi import HTTPException  # noqa: E402

from livestack_node.hostbroker import HostBroker  # noqa: E402
from livestack_node.hostd import _release_report, build_app  # noqa: E402


class _StatusPeer:
    def __init__(self, report=None, error=None):
        self.report = report
        self.error = error
        self.refresh_calls = 0

    def refresh(self):
        self.refresh_calls += 1
        if self.error:
            raise self.error
        return self.report


def _status_client(*peers):
    broker = HostBroker([], [], clock=lambda: 1000.0)
    broker.peers = list(peers)
    return build_app(broker)


def _get(app, path):
    async def request():
        async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver") as client:
            return await client.get(path)
    return asyncio.run(request())


def test_status_sums_known_zero_and_omits_unknown_queue_depths():
    first = _StatusPeer({"units": [
        {"kind": "align", "queue": {"waiting": 0}},
        {"kind": "asr", "queue": {"waiting": 2}},
        {"kind": "tts", "queue": {}},
    ]})
    second = _StatusPeer({"units": [
        {"kind": "align", "queue": {"waiting": 3}},
        {"kind": "asr", "queue": {"waiting": 0}},
        {"kind": "tts", "queue": {"waiting": True}},
    ]})

    response = _get(_status_client(first, second), "/status")
    assert response.status_code == 200
    counters = response.json()["counters"]
    by_kind = counters["by_kind"]
    assert by_kind["align"]["queue_depth"] == 3
    assert by_kind["asr"]["queue_depth"] == 2
    assert "queue_depth" not in by_kind["tts"]
    assert counters["queue_depth_complete"] is True
    assert first.refresh_calls == second.refresh_calls == 1


def test_status_marks_all_queue_depths_unknown_when_a_peer_refresh_fails():
    good = _StatusPeer({"units": [
        {"kind": "align", "queue": {"waiting": 0}},
    ]})
    down = _StatusPeer(error=ConnectionError("peer down"))

    response = _get(_status_client(good, down), "/status")
    assert response.status_code == 200
    counters = response.json()["counters"]
    assert counters["queue_depth_complete"] is False
    assert "queue_depth" not in counters["by_kind"]["align"]
    assert good.refresh_calls == down.refresh_calls == 1


def test_release_wall_duration_has_a_finite_365_day_bound():
    assert _release_report({"wall_s": 365 * 24 * 60 * 60})["job_wall_s"] == 365 * 24 * 60 * 60
    with pytest.raises(HTTPException) as error:
        _release_report({"wall_s": 365 * 24 * 60 * 60 + 1})
    assert error.value.status_code == 422
