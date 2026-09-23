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
