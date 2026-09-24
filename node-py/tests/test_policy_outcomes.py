"""A policy-granted lease's outcome is appended to the policy record stream
when it ends (scheduler-policy-routine task 3.3, Jingway design §6.3)."""
import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from livestack_node.hostbroker import HostBroker  # noqa: E402
from livestack_node.hostd import build_app  # noqa: E402
from livestack_node.policy_runtime import PolicyRuntime  # noqa: E402
from policy_fakes import FakeRecorder  # noqa: E402

DID = "01JXXXXXXXXXXXXXXXXXXXXXXX"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _broker(tmp_path, clock):
    b = HostBroker(clock=clock)
    b.hosted_lease_ttl_s = 120.0
    rec = FakeRecorder()
    b.policy_runtime = PolicyRuntime(str(tmp_path), native=None, recorder=rec)
    return b, rec


def _outcomes(rec):
    return {(o["decision_id"], o["outcome_id"]): (o["value"], o["source"])
            for o in rec.of("policy_outcome")}


def test_release_without_a_body_records_held_and_not_expired(tmp_path):
    clock = Clock()
    b, rec = _broker(tmp_path, clock)
    lid = b.hosted_checkout("http://n:8100", "llm", "o", decision_id=DID)
    clock.t += 41.5
    client = TestClient(build_app(b))
    assert client.post(f"/lease/{lid}/release").json() == {"ok": True}
    assert _outcomes(rec) == {(DID, "lease_held_s"): (41.5, "hostd.lease_release"),
                              (DID, "lease_expired"): (0.0, "hostd.lease_release")}


def test_release_with_a_body_adds_what_the_caller_reported(tmp_path):
    clock = Clock()
    b, rec = _broker(tmp_path, clock)
    lid = b.hosted_checkout("http://n:8100", "llm", "o", decision_id=DID)
    clock.t += 50
    client = TestClient(build_app(b))
    r = client.post(f"/lease/{lid}/release", json={"status": "ok", "wall_s": 41.2})
    assert r.json() == {"ok": True}
    o = _outcomes(rec)
    assert o[(DID, "caller_ok")][0] == 1.0 and o[(DID, "job_wall_s")][0] == 41.2
    assert o[(DID, "lease_expired")][0] == 0.0 and o[(DID, "lease_held_s")][0] == 50.0
    lid2 = b.hosted_checkout("http://n:8100", "llm", "o", decision_id="01JOTHER")
    client.post(f"/lease/{lid2}/release", json={"status": "failed"})
    assert _outcomes(rec)[("01JOTHER", "caller_ok")][0] == 0.0
    assert ("01JOTHER", "job_wall_s") not in _outcomes(rec)


@pytest.mark.parametrize("body", [
    {"status": "done"}, {"status": 1}, {"wall_s": "41"}, {"wall_s": -1},
    {"wall_s": True}, {"status": "ok", "wall_s": None},
])
def test_a_bad_body_is_422_and_the_lease_is_kept(tmp_path, body):
    b, rec = _broker(tmp_path, Clock())
    lid = b.hosted_checkout("http://n:8100", "llm", "o", decision_id=DID)
    r = TestClient(build_app(b)).post(f"/lease/{lid}/release", json=body)
    assert r.status_code == 422
    assert lid in b.hosted_leases and rec.lines == []


def test_expiry_records_expired_and_the_time_it_was_held_until_it_lapsed(tmp_path):
    clock = Clock()
    b, rec = _broker(tmp_path, clock)
    b.hosted_checkout("http://n:8100", "llm", "o", decision_id=DID)
    clock.t += 10
    # reaped lazily, long after it lapsed: held is 120 s, not 1000 s
    clock.t += 990
    assert b.owner_usage() == {}
    o = _outcomes(rec)
    assert o[(DID, "lease_expired")] == (1.0, "hostd.lease_expiry")
    assert o[(DID, "lease_held_s")][0] == 120.0
    # no caller_ok is invented for an expiry
    assert (DID, "caller_ok") not in o and (DID, "job_wall_s") not in o


def test_a_heartbeat_past_ttl_is_an_expiry(tmp_path):
    clock = Clock()
    b, rec = _broker(tmp_path, clock)
    lid = b.hosted_checkout("http://n:8100", "llm", "o", decision_id=DID)
    clock.t += 121
    assert b.hosted_heartbeat(lid) is False
    assert _outcomes(rec)[(DID, "lease_expired")][0] == 1.0


def test_every_reap_site_emits_once(tmp_path):
    clock = Clock()
    b, rec = _broker(tmp_path, clock)
    b.hosted_checkout("http://n:8100", "llm", "o", decision_id=DID)
    clock.t += 500
    b.leases_on("http://n:8100")
    b.owner_usage()
    b._hosted_placements()
    assert len(rec.of("policy_outcome")) == 2


def test_a_lease_without_a_decision_id_emits_nothing(tmp_path):
    clock = Clock()
    b, rec = _broker(tmp_path, clock)
    lid = b.hosted_checkout("http://n:8100", "llm", "o")
    TestClient(build_app(b)).post(f"/lease/{lid}/release", json={"status": "ok"})
    lid = b.hosted_checkout("http://n:8100", "llm", "o")
    clock.t += 500
    b.owner_usage()
    assert rec.lines == []


def test_a_host_broker_without_a_runtime_releases_as_before(tmp_path):
    b = HostBroker(clock=Clock())
    lid = b.hosted_checkout("http://n:8100", "llm", "o", decision_id=DID)
    assert TestClient(build_app(b)).post(f"/lease/{lid}/release",
                                         json={"status": "ok", "wall_s": 3}).json() == {"ok": True}


def test_outcomes_are_stamped_with_wall_time_not_the_lease_clock(tmp_path):
    # The lease clock is `time.monotonic()` in production (seconds since boot).
    # Durations use it; timestamps must not, or the improver's time window never
    # joins an outcome to its decision. Found on the first live deploy.
    import time
    clock = Clock(t=1_564_358.0)
    b, rec = _broker(tmp_path, clock)
    lid = b.hosted_checkout("http://n:8100", "llm", "o", decision_id=DID)
    clock.t += 5
    before = time.time()
    TestClient(build_app(b)).post(f"/lease/{lid}/release")
    stamps = [o["ts"] for o in rec.of("policy_outcome")]
    assert stamps and all(before - 1 <= ts <= time.time() + 1 for ts in stamps)
    assert _outcomes(rec)[(DID, "lease_held_s")][0] == 5.0
