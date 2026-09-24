"""/fleet/plan decides greedily under the active policy artifact and names it
(scheduler-policy-routine task 3.2, design §5/§6)."""
import json
import os
import random

from livestack_node.fleet_operations import OperationStore
from livestack_node.fleet_ops_api import build_plan, plan_is_current
from livestack_node.fleet_scheduler import SchedulerPolicy
from livestack_node.policy_runtime import POLICY_ID, PolicyRuntime
from policy_fakes import FakeNative, FakeRecorder, artifact

NOW = 1_788_600_000.0


def _node(ip, host, in_flight):
    return {"peer": f"http://{ip}:8100/livestack", "state": "fresh", "ready": True,
            "kinds": ["llm"], "detail": "resident", "unseen_seconds": 0.0,
            "device_id": f"{host}/dev", "load": {"in_flight": in_flight}}


VIEW = {"generated_at": NOW, "hosts": {
    "a": {"nodes": [_node("100.64.0.18", "a", 0)]},
    "b": {"nodes": [_node("100.64.0.3", "b", 1)]},
    "c": {"nodes": [_node("100.64.0.2", "c", 1)]}}}

EXPLORING = {"enabled": True, "epsilon": 0.1, "margin": 5.0}


class RandomExplorer(FakeNative):
    """Explores on EVERY decision, picking uniformly at random: if the plan path
    did not force greedy, ten plans would not agree."""

    def decide(self, handle, ctx, candidates, decision_id):
        d = super().decide(handle, ctx, candidates, decision_id)
        elig = [r["id"] for r in d["rows"] if r["eligible"]]
        pick = random.choice(elig)
        return {**d, "chosen": pick, "explored": pick != d["greedy"],
                "explore_set": elig,
                "propensities": {i: 1 / len(elig) for i in elig},
                "exploration": {**EXPLORING, "draw": random.random()}}


def _write(d, art):
    path = os.path.join(d, f"{POLICY_ID}.active.json")
    with open(path, "w") as fh:
        json.dump(art, fh)
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))


def _plan(rt, store):
    return build_plan(VIEW, [{"job_id": "j1", "kind": "llm", "sla": "normal"}],
                      owner="o", policy=SchedulerPolicy(), pools=(), store=store,
                      usage={}, now=NOW, runtime=rt)


def test_ten_plans_under_an_exploring_artifact_choose_the_same_target(tmp_path):
    _write(tmp_path, artifact(exploration=EXPLORING))
    rt = PolicyRuntime(str(tmp_path), native=RandomExplorer(), recorder=FakeRecorder())
    store = OperationStore(str(tmp_path / "ops.sqlite3"))
    plans = [_plan(rt, store) for _ in range(10)]
    chosen = {p["actions"][0]["target_id"] for p in plans}
    assert len(chosen) == 1
    assert all(p["exploration"] == "off_on_plan_path" for p in plans)
    assert plans[0]["artifact_version"] == rt.artifact_version
    assert rt.recorder.lines == []            # the plan path records nothing
    # positive control: the same runtime DOES explore when not on the plan path
    from livestack_node.fleet_admit import admit
    seen = {admit(VIEW, kind="llm", owner="o", now=NOW, runtime=rt,
                  decision_id=f"01J{i:023d}")["target"]["target_id"] for i in range(40)}
    assert len(seen) > 1


def test_the_digest_changes_when_the_artifact_changes(tmp_path):
    _write(tmp_path, artifact())
    clock = [0.0]
    rt = PolicyRuntime(str(tmp_path), native=FakeNative(), recorder=FakeRecorder(),
                       clock=lambda: clock[0])
    store = OperationStore(str(tmp_path / "ops.sqlite3"))
    first = _plan(rt, store)
    assert plan_is_current(first["plan_version"], policy=SchedulerPolicy(), pools=(),
                           now=NOW, artifact_version=rt.artifact_version) is None
    _write(tmp_path, artifact(w_distance=0.0))
    clock[0] += 6
    rt.reload_if_changed()
    second = _plan(rt, store)
    assert first["plan_version"] != second["plan_version"]
    assert first["artifact_version"] != second["artifact_version"]
    # a plan taken under the old artifact is now stale
    assert "policy" in plan_is_current(first["plan_version"], policy=SchedulerPolicy(),
                                       pools=(), now=NOW,
                                       artifact_version=rt.artifact_version)


def test_without_a_runtime_the_digest_is_unchanged(tmp_path):
    store = OperationStore(str(tmp_path / "ops.sqlite3"))
    p = _plan(None, store)
    from livestack_node.fleet_ops_api import policy_digest
    assert p["plan_version"].split(".")[1] == policy_digest(SchedulerPolicy(), ())
    assert p["artifact_version"] is None
