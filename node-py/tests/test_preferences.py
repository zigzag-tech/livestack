import json

import pytest

from livestack_node.fleet_rank import rank
from livestack_node.preferences import PreferenceError, parse_preferences


NOW = 1_790_000_000.0


def _node(name, inventory, ms=5):
    return {"peer": f"http://{name}/livestack", "state": "fresh", "ready": True,
            "kinds": ["polyasr"], "probe_ms": ms, "inventory": inventory,
            "load": {"in_flight": 0, "pressure": .2}}


def _metric(value, *, evaluation="benchday-v1", revision="r1", cohort="en-live",
            samples=50, measured=NOW, ttl=3600, model="weights-a"):
    return {"value": value, "evaluation": evaluation, "evaluation_revision": revision,
            "cohort": cohort, "sample_count": samples, "measured_at": measured,
            "ttl_s": ttl, "model_revision": model}


def _view(nodes):
    return {"generated_at": NOW, "hosts": {"h": {"nodes": nodes}}}


def test_parser_is_bounded_typed_and_backward_compatible():
    assert parse_preferences(None) == []
    raw = json.dumps([{"attribute": "asr.streaming", "value": "native"}])
    assert parse_preferences(raw)[0]["value"] == "native"
    with pytest.raises(PreferenceError):
        parse_preferences("not-json")
    with pytest.raises(PreferenceError):
        parse_preferences(json.dumps([{"attribute": "model.vendor", "value": "x"}]))
    with pytest.raises(PreferenceError):
        parse_preferences(json.dumps([{"attribute": "asr.streaming", "value": "native"}] * 9))


def test_native_streaming_can_beat_nearer_synthetic_without_excluding_fallback():
    near = _node("near", {"asr": {"streaming": "synthetic"}}, ms=2)
    native = _node("native", {"asr": {"streaming": "native"}}, ms=40)
    prefer = parse_preferences(json.dumps([
        {"attribute": "asr.streaming", "value": "native"}]))
    result = rank(_view([near, native]), "polyasr", now=NOW, prefer=prefer)
    assert [x["target_id"] for x in result["targets"]] == ["http://native", "http://near"]
    assert len(result["targets"]) == 2
    assert result["targets"][0]["preferences"][0]["matched"] is True


def test_quality_requires_same_evaluation_and_unknown_never_wins():
    clause = parse_preferences(json.dumps([{
        "metric": "asr.quality", "direction": "max", "evaluation": "benchday-v1",
        "evaluation_revision": "r1", "cohort": "en-live", "min_samples": 20}]))
    high = _node("high", {"asr": {"quality": _metric(.91)}})
    low = _node("low", {"asr": {"quality": _metric(.82)}})
    alien = _node("alien", {"asr": {"quality": _metric(.99, revision="r2")}})
    silent = _node("silent", {"asr": {}})
    result = rank(_view([low, silent, alien, high]), "polyasr", now=NOW, prefer=clause)
    assert [x["target_id"] for x in result["targets"]][:2] == ["http://high", "http://low"]
    receipts = {x["target_id"]: x["preferences"][0] for x in result["targets"]}
    assert receipts["http://alien"]["reason"] == "incomparable evaluation_revision"
    assert receipts["http://silent"]["reason"] == "no observation"


def test_stale_or_undersampled_quality_is_no_opinion():
    clause = parse_preferences(json.dumps([{
        "metric": "asr.quality", "direction": "max", "evaluation": "benchday-v1",
        "evaluation_revision": "r1", "cohort": "en-live", "min_samples": 20}]))
    stale = _node("stale", {"asr": {"quality": _metric(.99, measured=NOW-4000)}})
    tiny = _node("tiny", {"asr": {"quality": _metric(.99, samples=2)}})
    result = rank(_view([stale, tiny]), "polyasr", now=NOW, prefer=clause)
    reasons = {x["target_id"]: x["preferences"][0]["reason"] for x in result["targets"]}
    assert reasons == {"http://stale": "stale or unbounded", "http://tiny": "undersampled"}


# -- distance first, preference as tie-break (owner rule 2026-11-04) ----------
#
# The fleet as the owner's Nanjing phone sees it: xc-mac-studio and zz-tower0
# in Nanjing (~20 ms), the Vaughan tower ~1.3 s away. Only Vaughan runs R2T2 and
# advertises native streaming. With the preference ahead of distance, 159 of
# 231 dictations (Nov 8-14) started in Canada.

NATIVE = {"asr": {"streaming": "native", "backend": "r2t2"}}
SYNTHETIC = {"asr": {"streaming": "synthetic", "backend": "qwen"}}
LIVE_PREFER = parse_preferences(json.dumps([
    {"attribute": "asr.streaming", "value": "native"},
    {"metric": "asr.first_partial_ms", "direction": "min", "min_samples": 10},
    {"metric": "asr.finalization_ms", "direction": "min", "min_samples": 10}]))


def _china_fleet(mac_load=None, tower0_load=None, vaughan_load=None):
    def n(peer, inv, load, region):
        return {"peer": peer, "state": "fresh", "ready": True, "kinds": ["asr"],
                "region": region,
                "inventory": inv,
                "load": load if load is not None else {"in_flight": 0}}
    return {"generated_at": NOW, "hosts": {
        "xc-tower-ubuntu": {"nodes": [
            n("http://100.64.0.18:8766/livestack", NATIVE, vaughan_load, "na")],
            "links": {"xc-mac-studio": 260.0, "zz-tower0": 1300.0}},
        "xc-mac-studio": {"nodes": [
            n("http://100.64.0.2:8765/livestack", SYNTHETIC, mac_load, "asia-cn")],
            "links": {"zz-tower0": 20.0, "xc-tower-ubuntu": 1300.0}},
        "zz-tower0": {"nodes": [
            n("http://100.64.0.3:8766/livestack", SYNTHETIC, tower0_load, "asia-cn")],
            "links": {"xc-mac-studio": 20.0, "xc-tower-ubuntu": 1300.0}},
        "zz-tower2": {"nodes": [],
                      "links": {"xc-mac-studio": 20.0, "zz-tower0": 20.0,
                                "xc-tower-ubuntu": 1300.0}},
    }}


@pytest.mark.parametrize("vantage", ["host:zz-tower2", "region:asia-cn"])
def test_a_china_client_does_not_cross_the_pacific_for_native_streaming(vantage):
    r = rank(_china_fleet(), "asr", vantage=vantage, now=NOW, prefer=LIVE_PREFER)
    order = [t["target_id"] for t in r["targets"]]
    assert order[0] in ("http://100.64.0.2:8765", "http://100.64.0.3:8766"), order
    assert order[-1] == "http://100.64.0.18:8766", "Vaughan is the far fallback"
    assert len(order) == 3, "the native engine stays available for failover"


def test_native_streaming_still_breaks_a_tie_inside_one_band():
    """Roughly equally close = the same distance band: there the preference
    decides, which is what the existing 2 ms vs 40 ms test also pins."""
    fleet = _china_fleet()
    fleet["hosts"]["zz-tower0"]["nodes"][0]["inventory"] = NATIVE
    r = rank(fleet, "asr", vantage="host:zz-tower2", now=NOW, prefer=LIVE_PREFER)
    assert r["targets"][0]["target_id"] == "http://100.64.0.3:8766"


def test_a_busy_native_engine_loses_to_a_free_one_in_the_same_band():
    fleet = _china_fleet(tower0_load={"in_flight": 3})
    fleet["hosts"]["zz-tower0"]["nodes"][0]["inventory"] = NATIVE
    r = rank(fleet, "asr", vantage="host:zz-tower2", now=NOW, prefer=LIVE_PREFER)
    assert r["targets"][0]["target_id"] == "http://100.64.0.2:8765"


def test_a_near_busy_engine_still_beats_a_far_idle_native_one():
    """Distance first, load second: a queue in Nanjing is cheaper than 1.3 s."""
    r = rank(_china_fleet(mac_load={"in_flight": 3}, tower0_load={"in_flight": 3}),
             "asr", vantage="host:zz-tower2", now=NOW, prefer=LIVE_PREFER)
    assert r["targets"][-1]["target_id"] == "http://100.64.0.18:8766"
