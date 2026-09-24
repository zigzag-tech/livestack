"""The pure-Python reference of `livestack.fleet.choose_target` v1
(scheduler-policy-routine design §2.4): one test per reason code, plus the
normalisation and tie-break rules the native family must reproduce exactly."""
import pytest

from livestack_node.policy_runtime import (
    DEFAULT_PARAMS, choose_target_reference, greedy_decision,
)

NOW = 1_788_600_000.0


def ctx(sla="normal", deadline=None, created_at=NOW, est=60.0, locality=None):
    return {"now": NOW, "job": {"id": "j", "sla": sla, "created_at": created_at,
                                "deadline": deadline, "est_duration_s": est,
                                "locality_host": locality}}


def cand(id, *, tier="LOCAL", running=True, elastic=False, selector_match=True,
         fits_now=True, headroom_ok=False, fits_instance=False, latency=0.0,
         per_hour=0.0, per_job=0.0, distance=None, util=None, host=None):
    return {"id": id, "features": {
        "host_id": host or f"h-{id}", "tier": tier, "running": running,
        "elastic": elastic, "selector_match": selector_match, "fits_now": fits_now,
        "headroom_ok": headroom_ok, "fits_instance": fits_instance,
        "provision_latency_s": latency, "cost_per_hour": per_hour,
        "cost_per_job": per_job, "distance_ms": distance, "utilization": util}}


def pool(id, tier="SPOT", **kw):
    kw.setdefault("headroom_ok", True)
    kw.setdefault("fits_instance", True)
    return cand(id, tier=tier, running=False, elastic=True, fits_now=False, **kw)


def rows_of(cands, c=None, params=DEFAULT_PARAMS):
    return {r["id"]: r for r in choose_target_reference(params, c or ctx(), cands)}


def code(row):
    return row["reason"].split(" ")[0]


# --- one per reason code ------------------------------------------------------
def test_selector_mismatch():
    r = rows_of([cand("a", selector_match=False), cand("b")])
    assert code(r["a"]) == "filtered:selector" and not r["a"]["eligible"]
    assert r["a"]["score"] is None and r["a"]["explorable"] is False


def test_deadline_uses_sla_slack_when_no_explicit_deadline():
    # interactive slack is 30 s; a job created 40 s ago cannot start anywhere.
    r = rows_of([cand("a")], ctx(sla="interactive", created_at=NOW - 40))
    assert code(r["a"]) == "filtered:deadline"
    # an explicit deadline wins over the SLA slack
    r = rows_of([cand("a")], ctx(sla="interactive", created_at=NOW - 40,
                                 deadline=NOW + 1))
    assert r["a"]["eligible"]


def test_deadline_counts_provision_latency_for_a_pool():
    r = rows_of([pool("p", latency=100.0)], ctx(deadline=NOW + 99))
    assert code(r["p"]) == "filtered:deadline"


def test_no_room_on_a_running_target():
    assert code(rows_of([cand("a", fits_now=False)])["a"]) == "filtered:no_room"


def test_pool_at_cap():
    assert code(rows_of([pool("p", headroom_ok=False)])["p"]) == "filtered:pool_at_cap"


def test_instance_too_small():
    r = rows_of([pool("p", fits_instance=False)])
    assert code(r["p"]) == "filtered:instance_too_small"


def test_cold_not_elastic():
    r = rows_of([cand("a", running=False, elastic=False, fits_now=False)])
    assert code(r["a"]) == "filtered:cold_not_elastic"


def test_last_resort_guard_while_cheaper_tier_is_feasible():
    r = rows_of([pool("runpod", tier="LAST_RESORT"), pool("spot")])
    assert code(r["runpod"]) == "filtered:last_resort_guard"
    assert r["spot"]["eligible"]


def test_last_resort_eligible_when_nothing_cheaper_is():
    r = rows_of([pool("runpod", tier="LAST_RESORT"), cand("a", fits_now=False)])
    assert r["runpod"]["eligible"]
    # never explorable, even as the only choice
    assert r["runpod"]["explorable"] is False


def test_eligible_reason_names_every_term():
    r = rows_of([cand("a"), cand("b", distance=5.0)])
    assert r["a"]["reason"].startswith("scored:")
    for term in ("cost_n=", "eta_n=", "dist_n=", "util_n=", "local="):
        assert term in r["a"]["reason"]


# --- normalisation rules ------------------------------------------------------
def test_utilization_unreported_scores_as_the_upper_median():
    # known [0.0, 0.5, 1.0, 1.0] -> sorted[4 // 2] = 1.0 (UPPER median), so the
    # silent target scores exactly like the busiest, not like 0.5.
    base = dict(DEFAULT_PARAMS, w_distance=0.0, w_resource=0.0)
    cs = [cand("u0", util=0.0), cand("u5", util=0.5), cand("u1a", util=1.0),
          cand("u1b", util=1.0), cand("silent")]
    r = rows_of(cs, params=base)
    assert r["silent"]["score"] == r["u1a"]["score"] == pytest.approx(1.0)
    assert r["u5"]["score"] == pytest.approx(0.5)


def test_unmeasured_distance_scores_as_the_farthest_measured():
    base = dict(DEFAULT_PARAMS, w_utilization=0.0, w_resource=0.0)
    r = rows_of([cand("near", distance=2.0), cand("far", distance=700.0),
                 cand("unknown")], params=base)
    assert r["unknown"]["score"] == r["far"]["score"]
    assert r["near"]["score"] < r["far"]["score"]


def test_no_measured_distance_contributes_nothing():
    r = rows_of([cand("a"), cand("b")])
    assert r["a"]["score"] == r["b"]["score"]


def test_distance_weight_is_scaled_by_sla():
    cs = [cand("near", distance=1.0, util=1.0), cand("far", distance=100.0, util=0.0)]
    inter = rows_of(cs, ctx(sla="interactive"))
    batch = rows_of(cs, ctx(sla="batch"))
    assert inter["near"]["score"] < inter["far"]["score"]      # 2.0 x 1.0 vs 1.0
    assert batch["far"]["score"] < batch["near"]["score"]      # 2.0 x 0.1 vs 1.0


def test_local_and_locality_bonus():
    r = rows_of([cand("spot", tier="SPOT", host="x"), cand("here", tier="SPOT", host="me")],
                ctx(locality="me"))
    assert r["here"]["score"] == pytest.approx(r["spot"]["score"] - 0.5)
    r = rows_of([cand("local", tier="LOCAL"), cand("spot", tier="SPOT")])
    assert r["local"]["score"] == pytest.approx(r["spot"]["score"] - 1.0)


# --- choice ---------------------------------------------------------------------
def test_tie_goes_to_the_earliest_input_position():
    cs = [cand("z"), cand("a"), cand("m")]
    rows = choose_target_reference(DEFAULT_PARAMS, ctx(), cs)
    assert len({r["score"] for r in rows}) == 1
    d = greedy_decision(rows, decision_id="d", artifact_version="v", ctx=ctx(),
                        candidates=cs)
    assert d["greedy"] == d["chosen"] == "z"
    assert d["propensities"] == {"z": 1.0} and d["explore_set"] == ["z"]


def test_no_eligible_candidate_chooses_none():
    rows = choose_target_reference(DEFAULT_PARAMS, ctx(), [cand("a", fits_now=False)])
    d = greedy_decision(rows, decision_id="d", artifact_version="v", ctx=ctx(),
                        candidates=[])
    assert d["chosen"] is None and d["explore_set"] == [] and d["propensities"] == {}


def test_explorable_only_for_running_non_last_resort():
    r = rows_of([cand("run"), pool("spot")])
    assert r["run"]["explorable"] is True
    assert r["spot"]["eligible"] and r["spot"]["explorable"] is False


def test_rows_are_in_input_order_one_per_candidate():
    cs = [cand("c"), pool("b", headroom_ok=False), cand("a", selector_match=False)]
    rows = choose_target_reference(DEFAULT_PARAMS, ctx(), cs)
    assert [r["id"] for r in rows] == ["c", "b", "a"]
