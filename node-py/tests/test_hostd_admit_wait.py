"""POST /admit with wait_s: the broker holds a deferred request until it is placed.

A refusal inside the window is not an answer. The request is planned again as
the fleet changes, requests for the same requirement are served in arrival
order, and a request that out-waits its window is refused with the planner\x27s
last reason. wait_s absent (or 0) is today\x27s single-shot behaviour.
"""
import threading
import time

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node import hostd  # noqa: E402
from livestack_node.hostd import build_app  # noqa: E402
from livestack_node.planner import Defer, Grant  # noqa: E402


class _Plan:
    def __init__(self, actions=()):
        self.actions = list(actions)

    def of(self, cls):
        return [a for a in self.actions if isinstance(a, cls)]

    def summary(self):
        return {"actions": len(self.actions)}


class _Broker:
    peers = []
    device_config = {}
    fleet_principals = None

    def __init__(self, grant_after):
        self.grant_after = grant_after   # how many passes defer before one grants
        self.passes = 0
        self.order = []                  # request ids in the order they were granted

    def plan_and_apply(self, requests, _last):
        req = requests[0]
        self.passes += 1
        if self.passes > self.grant_after:
            self.order.append(req.id)
            return _Plan([Grant(request_id=req.id, kind="qwen-image", device_id="gpu0")])
        return _Plan([Defer(request_id=req.id, reason="no unit satisfies {params_b>=20}")])

    def emit_admit(self, *a, **k):
        pass


def _client(monkeypatch, broker):
    monkeypatch.setenv("LIVESTACK_REPLAN_INTERVAL", "0")
    monkeypatch.setattr(hostd, "ADMIT_WAIT_POLL_S", 0.01)
    return TestClient(build_app(broker), raise_server_exceptions=False)


BODY = {"requires": {"class": "image", "params_b": "[20,30)"}}


def test_without_wait_s_a_deferral_is_answered_at_once(monkeypatch):
    broker = _Broker(grant_after=3)
    r = _client(monkeypatch, broker).post("/admit", json=BODY)
    assert r.json()["granted"] is False
    assert broker.passes == 1


def test_wait_s_holds_the_request_until_it_is_placed(monkeypatch):
    broker = _Broker(grant_after=3)
    r = _client(monkeypatch, broker).post("/admit", json={**BODY, "wait_s": 5})
    assert r.json()["granted"] is True
    assert r.json()["device_id"] == "gpu0"
    assert broker.passes == 4


def test_a_request_that_outwaits_its_window_gets_the_planners_last_reason(monkeypatch):
    broker = _Broker(grant_after=10**9)
    r = _client(monkeypatch, broker).post("/admit", json={**BODY, "wait_s": 0.2})
    out = r.json()
    assert out["granted"] is False
    assert "no unit satisfies" in out["defer_reason"]
    assert broker.passes > 1


@pytest.mark.parametrize("bad", [-1, 3601, "soon"])
def test_a_bad_wait_s_is_refused(monkeypatch, bad):
    broker = _Broker(grant_after=0)
    r = _client(monkeypatch, broker).post("/admit", json={**BODY, "wait_s": bad})
    assert r.status_code == 400
    assert broker.passes == 0


def test_held_requests_for_one_requirement_are_served_in_arrival_order(monkeypatch):
    broker = _Broker(grant_after=5)
    client = _client(monkeypatch, broker)
    answers = {}

    def ask(i):
        answers[i] = client.post("/admit", json={**BODY, "id": f"r{i}", "wait_s": 10}).json()

    threads = []
    for i in range(3):
        t = threading.Thread(target=ask, args=(i,))
        t.start()
        threads.append(t)
        time.sleep(0.15)             # arrival order is r0, r1, r2
    for t in threads:
        t.join(15)
    assert all(a["granted"] for a in answers.values())
    assert broker.order == ["r0", "r1", "r2"]
