"""PolicyRuntime (scheduler-policy-routine task 2.2): artifact loading,
reload interval, native/reference modes, compare-mode mismatches, shadows."""
import json
import os

from livestack_node.policy_runtime import (
    DEFAULTS_VERSION, MAX_MISMATCH_FILES, POLICY_ID, PolicyRuntime,
)
from policy_fakes import FakeNative, FakeRecorder, artifact

NOW = 1_788_600_000.0


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ctx():
    return {"now": NOW, "job": {"id": "j", "sla": "normal", "created_at": NOW,
                                "deadline": None, "est_duration_s": 60.0,
                                "locality_host": None}}


def c(id, distance=None, util=None, tier="LOCAL"):
    return {"id": id, "features": {
        "host_id": id, "tier": tier, "running": True, "elastic": False,
        "selector_match": True, "fits_now": True, "headroom_ok": False,
        "fits_instance": False, "provision_latency_s": 0.0, "cost_per_hour": 0.0,
        "cost_per_job": 0.0, "distance_ms": distance, "utilization": util}}


# near is busy, far is idle: w_distance decides between them.
CANDS = [c("near", distance=1.0, util=1.0), c("far", distance=100.0, util=0.0)]


def write(d, role, obj):
    path = os.path.join(d, f"{POLICY_ID}.{role}.json")
    with open(path, "w") as fh:
        fh.write(obj if isinstance(obj, str) else json.dumps(obj))
    # distinct mtimes even on a coarse filesystem clock
    st = os.stat(path)
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 10_000_000_000))
    return path


def rt(tmp_path, **kw):
    kw.setdefault("native", None)
    kw.setdefault("recorder", FakeRecorder())
    kw.setdefault("mode", "auto")
    logs = []
    r = PolicyRuntime(str(tmp_path), log=logs.append, **kw)
    r.logs = logs
    return r


def test_no_file_decides_on_defaults_and_says_so(tmp_path):
    r = rt(tmp_path)
    d = r.decide(ctx(), CANDS, "d1")
    assert d["artifact_version"] == DEFAULTS_VERSION
    assert d["chosen"] == "near"          # normal SLA: distance 2.0*0.5 vs util 1.0 tie -> earliest
    s = r.status()
    assert s["source"] == "defaults"
    assert "policy_artifact_missing" in s["degraded"]
    assert "policy_native_unavailable" in s["degraded"]


def test_a_file_changes_the_params(tmp_path):
    write(tmp_path, "active", artifact(w_distance=0.0))
    r = rt(tmp_path)
    assert r.decide(ctx(), CANDS, "d1")["chosen"] == "far"
    s = r.status()
    assert s["source"] == "file" and "policy_artifact_missing" not in s["degraded"]
    # without the native module the version is the file's own, unverified
    assert s["active"]["version_verified"] is False


def test_corrupt_file_after_a_good_one_keeps_the_good_one(tmp_path):
    clock = Clock()
    good = artifact(w_distance=0.0)
    write(tmp_path, "active", good)
    r = rt(tmp_path, clock=clock)
    assert r.artifact_version == good["version"]
    write(tmp_path, "active", "{ not json")
    clock.t += 6
    assert r.decide(ctx(), CANDS, "d2")["chosen"] == "far"
    assert r.artifact_version == good["version"]
    s = r.status()
    assert s["last_load_error"] and "policy_artifact_invalid" in s["degraded"]
    # one line naming the violation
    assert any("refused, keeping the previous artifact" in l for l in r.logs)
    # an out-of-bounds param names every violation, and is also refused
    bad = artifact(w_distance=0.0)
    bad["params"]["w_budget"] = 11
    bad["params"]["w_speed"] = -1
    write(tmp_path, "active", bad)
    clock.t += 6
    r.reload_if_changed()
    assert r.artifact_version == good["version"]
    assert "w_budget" in r.last_load_error and "w_speed" in r.last_load_error


def test_reload_waits_for_the_interval(tmp_path):
    clock = Clock()
    r = rt(tmp_path, clock=clock)
    write(tmp_path, "active", artifact(w_distance=0.0))
    clock.t += 4.9
    assert r.decide(ctx(), CANDS, "d1")["chosen"] == "near"   # not polled yet
    clock.t += 0.2
    assert r.decide(ctx(), CANDS, "d2")["chosen"] == "far"


def test_reload_rereads_only_a_changed_file(tmp_path, monkeypatch):
    clock = Clock()
    write(tmp_path, "active", artifact(w_distance=0.0))
    r = rt(tmp_path, clock=clock)
    reads = []
    real = r._reload_role
    monkeypatch.setattr(r, "_reload_role", lambda *a: (reads.append(a[0]), real(*a)))
    clock.t += 6
    r.reload_if_changed()
    assert reads == []


def test_compare_mode_acts_on_the_reference_and_counts_mismatches(tmp_path):
    r = rt(tmp_path, mode="compare", native=FakeNative(disagree=True))
    d = r.decide(ctx(), CANDS, "01JX")
    assert d["chosen"] == "near"          # the reference's choice
    assert r.mismatches == 1
    files = os.listdir(tmp_path / "mismatches")
    assert len(files) == 1 and "01JX" in files[0]
    case = json.loads((tmp_path / "mismatches" / files[0]).read_text())
    assert case["reference"]["greedy"] == "near" and case["native"]["greedy"] == "far"
    assert "policy_native_mismatch" in r.status()["degraded"]


def test_compare_mode_agreeing_native_counts_nothing(tmp_path):
    native = FakeNative()
    r = rt(tmp_path, mode="compare", native=native)
    r.decide(ctx(), CANDS, "d1")
    assert native.decisions == 1 and r.mismatches == 0
    assert "policy_native_mismatch" not in r.status()["degraded"]


def test_mismatch_files_are_bounded(tmp_path):
    r = rt(tmp_path, mode="compare", native=FakeNative(disagree=True))
    for i in range(MAX_MISMATCH_FILES + 7):
        r.decide(ctx(), CANDS, f"d{i:04d}")
    files = sorted(os.listdir(tmp_path / "mismatches"))
    assert len(files) == MAX_MISMATCH_FILES
    assert r.mismatches == MAX_MISMATCH_FILES + 7
    assert files[0].endswith("d0007.json")       # the oldest seven were deleted


def test_auto_mode_uses_native_when_present(tmp_path):
    native = FakeNative()
    r = rt(tmp_path, native=native)
    d = r.decide(ctx(), CANDS, "d1")
    assert native.decisions == 1
    assert d["artifact_version"].startswith("b3:fake")   # the defaults, versioned natively
    s = r.status()
    assert s["native"] is True and "policy_native_unavailable" not in s["degraded"]


def test_mode_0_never_calls_native(tmp_path):
    native = FakeNative()
    r = rt(tmp_path, native=native, mode="0")
    r.decide(ctx(), CANDS, "d1")
    assert native.decisions == 0


def test_shadow_never_changes_the_choice(tmp_path):
    write(tmp_path, "shadow", [artifact(w_distance=0.0)])
    for native in (None, FakeNative()):
        r = rt(tmp_path, native=native)
        d = r.decide(ctx(), CANDS, "d1")
        assert d["chosen"] == "near"
        assert [s["chosen"] for s in d["shadow"]] == ["far"]
        assert r.status()["shadow"][0]["version"].startswith("b3:fake")


def test_more_than_two_shadows_is_refused(tmp_path):
    write(tmp_path, "shadow", [artifact(w_distance=x) for x in (0.0, 1.0, 3.0)])
    r = rt(tmp_path)
    assert r.status()["shadow"] == [] and "shadow" in r.last_load_error


def test_exploration_is_off_without_the_native_module(tmp_path):
    write(tmp_path, "active", artifact(exploration={"enabled": True, "epsilon": 0.05,
                                                    "margin": 0.25}))
    r = rt(tmp_path)
    d = r.decide(ctx(), CANDS, "d1")
    assert d["explored"] is False and d["exploration"]["enabled"] is False
    s = r.status()
    assert s["exploration"] == "exploration_disabled:native_unavailable"
    assert "exploration_disabled:native_unavailable" in s["degraded"]


def test_without_exploration_forces_greedy(tmp_path):
    class Exploring(FakeNative):
        def decide(self, *a):
            d = super().decide(*a)
            return {**d, "chosen": "far", "explored": True,
                    "explore_set": ["near", "far"],
                    "propensities": {"near": 0.975, "far": 0.025}}
    r = rt(tmp_path, native=Exploring())
    assert r.decide(ctx(), CANDS, "d1")["chosen"] == "far"
    d = r.without_exploration().decide(ctx(), CANDS, "d1")
    assert d["chosen"] == d["greedy"] == "near"
    assert d["propensities"] == {"near": 1.0} and d["explored"] is False


def test_recorder_stats_and_drops_are_reported(tmp_path):
    rec = FakeRecorder(capacity=0)
    r = rt(tmp_path, recorder=rec)
    d = r.decide(ctx(), CANDS, "01J")
    assert r.record_decision(d, principal="x") is False
    s = r.status()
    assert s["records"]["stats"]["dropped"] == 1
    assert "policy_records_dropped" in s["degraded"]


def test_no_native_means_no_records(tmp_path):
    r = PolicyRuntime(str(tmp_path), native=None)
    s = r.status()
    assert s["records"]["unavailable"] == "native_unavailable"
    assert "policy_records_unavailable" in s["degraded"]


def test_a_refusal_is_not_recorded_but_counted(tmp_path):
    rec = FakeRecorder()
    r = rt(tmp_path, recorder=rec)
    full = [dict(c("a"), features={**c("a")["features"], "fits_now": False})]
    d = r.decide(ctx(), full, "d1")
    assert d["chosen"] is None
    assert r.record_decision(d, principal="p") is False
    assert rec.lines == [] and r.status()["skipped_no_choice"] == 1


def test_production_recorder_config_from_env(tmp_path):
    native = FakeNative()
    r = PolicyRuntime.from_env(env={"LIVESTACK_POLICY_DIR": str(tmp_path),
                                    "LIVESTACK_POLICY_RECORDS_MAX_MB": "256",
                                    "LIVESTACK_POLICY_RECORDS_FILES": "32",
                                    "LIVESTACK_POLICY_SELF_PRINCIPALS": "fleetd, improver"},
                               native=native)
    assert r.recorder.cfg == {"dir": str(tmp_path / "records"), "stem": POLICY_ID,
                              "max_file_bytes": 256 * 1024 * 1024, "max_files": 32,
                              "max_age_days": None}
    assert r.self_principals == {"fleetd", "improver"}
