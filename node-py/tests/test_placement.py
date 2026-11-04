"""placement/v1: a pluggable ranker behind a fixed problem, with default-v1 as
the baseline and the safety net (openspec harmony-speech-routing)."""
import json
import time

import pytest

from livestack_node import placement
from livestack_node.fleet_rank import rank
from livestack_node.placement_default import problem_from_fleet


class Log:
    def __init__(self):
        self.rows = []

    def record(self, **f):
        self.rows.append(f)


@pytest.fixture
def log(monkeypatch):
    lg = Log()
    placement.set_decision_log(lg)
    for v in ("HARMONY_PLACEMENT_RANKERS", "HARMONY_PLACEMENT_SHADOW"):
        monkeypatch.delenv(v, raising=False)
    yield lg
    placement.set_decision_log(None)
    for n in [n for n in placement.registered() if n != placement.DEFAULT_RANKER]:
        placement._REGISTRY.pop(n)


def node(peer, region="na", ready=True, in_flight=0, busy=None):
    n = {"peer": f"{peer}/livestack", "state": "fresh", "ready": ready, "kinds": ["polyasr"],
         "region": region, "detail": "x", "load": {"in_flight": in_flight},
         "units": [{"kind": "asr", "resident": True}]}
    if busy:
        n["device_busy"] = busy
    return n


def fleet(*pairs):
    """pairs of (host, node); the cn host measures the na hosts as far away."""
    hosts = {}
    for h, n in pairs:
        hosts.setdefault(h, {"nodes": [], "links": {}})["nodes"].append(n)
    hosts["cn1"] = {"nodes": [{"region": "cn", "peer": "http://x/livestack", "kinds": []}],
                    "links": {"a": 900.0, "b": 30.0}}
    return {"generated_at": 1000.0, "hosts": hosts}


def problem(view=None, **kw):
    view = view or fleet(("a", node("http://a")), ("b", node("http://b", in_flight=2)),
                         ("c", node("http://c", ready=False, busy="held by image")))
    p, result = problem_from_fleet(view, "polyasr", vantage=kw.pop("vantage", "region:cn"),
                                   require=None, prefer=[], regions=None, expected_tokens=None,
                                   now=1000.0, **kw)
    return p, result


def ids(decision):
    return [r["id"] for r in decision["ranking"]]


# -- baseline: default-v1 is fleet_rank ---------------------------------------

def test_default_v1_orders_exactly_as_fleet_rank_does(log):
    p, result = problem()
    d = placement.run(p)
    assert d["ranker"] == "default-v1" and d["fallback"] is None
    assert ids(d) == [t["target_id"] for t in result["targets"]] == ["http://b", "http://a"]  # cn vantage: b is near
    assert d["chosen"] == "http://b"
    assert d["ranking"][0]["explain"]["distance_band"] == "<50"


def test_hard_constraints_are_stamped_before_ranking_and_leased_nodes_are_ineligible(log):
    p, _ = problem()
    by = {c["id"]: c for c in p["candidates"]}
    assert by["http://c"]["eligible"] is False and "device busy" in by["http://c"]["ineligible_reason"]
    assert placement.eligible_ids(p) == ["http://b", "http://a"] or set(placement.eligible_ids(p)) == {"http://a", "http://b"}


# -- forward compatibility ----------------------------------------------------

def test_unknown_context_and_fact_fields_are_ignored_by_default_and_kept_in_the_log(log):
    p, _ = problem()
    base = ids(placement.run(json.loads(json.dumps(p))))
    p["context"]["net.rtt_ms"] = {"cn": {"http://a": 745}}
    p["context"]["acct.prefs"] = {"quality": "high"}
    p["candidates"][0]["facts"]["cost.usd_per_min"] = 0.02
    p["future_top_level"] = 1
    d = placement.run(p)
    assert ids(d) == base
    logged = [r for r in log.rows if r["event"] == "decision"][-1]["problem"]
    assert logged["context"]["net.rtt_ms"] and logged["future_top_level"] == 1
    assert logged["candidates"][0]["facts"]["cost.usd_per_min"] == 0.02


def test_a_ranker_can_use_a_new_context_feature_without_breaking_the_default(log, monkeypatch):
    p, _ = problem()
    p["context"]["net.rtt_ms"] = {"http://a": 10, "http://b": 700}

    def by_rtt(prob):
        rtt = prob["context"].get("net.rtt_ms") or {}
        order = sorted(placement.eligible_ids(prob), key=lambda i: rtt.get(i, 1e9))
        return {"ranking": [{"id": i, "score": -rtt.get(i, 1e9), "explain": {"net.rtt_ms": rtt.get(i)}} for i in order]}

    placement.register("rtt@1", by_rtt)
    monkeypatch.setenv("HARMONY_PLACEMENT_RANKERS", json.dumps({"polyasr": "rtt@1"}))
    d = placement.run(p)
    assert d["ranker"] == "rtt@1" and d["chosen"] == "http://a" and d["fallback"] is None
    # the same problem without the feature: the ranker has no opinion, still legal
    p2, _ = problem()
    assert placement.run(p2)["ranker"] == "rtt@1"


# -- misbehaving rankers fall back to default-v1, with a logged reason --------

def _slow(prob):
    time.sleep(1.0)
    return {"ranking": []}


def _boom(prob):
    raise RuntimeError("model file missing")


@pytest.mark.parametrize("name,fn,why", [
    ("boom@1", _boom, "RuntimeError"),
    ("slow@1", _slow, "timeout"),
    ("resurrect@1", lambda p: {"ranking": [{"id": "http://c", "score": 1}]}, "ineligible"),
    ("ghost@1", lambda p: {"ranking": [{"id": "http://nowhere", "score": 1}]}, "not a candidate"),
    ("dupe@1", lambda p: {"ranking": [{"id": "http://a"}, {"id": "http://a"}]}, "repeats"),
    ("garbage@1", lambda p: "nope", "not {ranking"),
    ("nan@1", lambda p: {"ranking": [{"id": "http://a", "score": float("nan")}]}, "score"),
])
def test_a_misbehaving_ranker_falls_back_to_the_default_and_says_why(log, monkeypatch, name, fn, why):
    placement.register(name, fn)
    monkeypatch.setenv("HARMONY_PLACEMENT_RANKERS", json.dumps({"*": name}))
    p, result = problem()
    d = placement.run(p, budget_s=0.05)
    assert d["ranker"] == "default-v1" and ids(d) == [t["target_id"] for t in result["targets"]]
    assert d["fallback"]["from"] == name and why in d["fallback"]["why"]
    row = [r for r in log.rows if r["event"] == "decision"][-1]
    assert row["fallback"]["from"] == name and row["ranker"] == "default-v1"


def test_an_unregistered_or_incompatible_ranker_falls_back_visibly(log, monkeypatch):
    monkeypatch.setenv("HARMONY_PLACEMENT_RANKERS", json.dumps({"polyasr": "missing@9"}))
    assert placement.run(problem()[0])["fallback"] == {"from": "missing@9", "why": "not registered"}
    placement.register("v2only@1", lambda p: {"ranking": []}, accepts=("placement/v2",))
    monkeypatch.setenv("HARMONY_PLACEMENT_RANKERS", json.dumps({"polyasr": "v2only@1"}))
    assert "not accepted" in placement.run(problem()[0])["fallback"]["why"]


def test_a_ranker_cannot_widen_the_attempt_policy_beyond_the_bounds(log, monkeypatch):
    placement.register("greedy@1", lambda p: {"ranking": [{"id": i} for i in placement.eligible_ids(p)],
                                              "attempt_policy": {"max_attempts": 99, "per_attempt_timeout_s": 2}})
    monkeypatch.setenv("HARMONY_PLACEMENT_RANKERS", json.dumps({"*": "greedy@1"}))
    pol = placement.run(problem()[0])["attempt_policy"]
    assert pol["max_attempts"] == placement.ATTEMPTS_MAX and pol["per_attempt_timeout_s"] == 2.0


# -- shadow and replay --------------------------------------------------------

def test_a_shadow_ranker_is_logged_and_never_acted_on(log, monkeypatch):
    placement.register("rev@1", lambda p: {"ranking": [{"id": i, "score": 0} for i in reversed(placement.eligible_ids(p))]})
    monkeypatch.setenv("HARMONY_PLACEMENT_SHADOW", json.dumps({"polyasr": "rev@1"}))
    d = placement.run(problem()[0])
    assert d["ranker"] == "default-v1" and d["chosen"] == "http://b"
    assert d["shadow"]["ranker"] == "rev@1" and [r["id"] for r in d["shadow"]["ranking"]][0] == "http://a"


def test_replay_scores_a_ranker_against_what_actually_succeeded(log):
    p, _ = problem()
    d = placement.run(p)
    placement.record_outcome(p["problem_id"], target="http://a", success=True, first_byte_ms=40.0)
    rows = [r for r in log.rows]
    default = placement.replay(rows, lambda prob: {"ranking": [{"id": i} for i in placement.eligible_ids(prob)]}, "input-order")
    assert default["scored"] == 1 and default["illegal"] == 0
    bad = placement.replay(rows, lambda prob: {"ranking": [{"id": "http://c"}]}, "bad")
    assert bad["illegal"] == 1 and bad["scored"] == 0
    out = [r for r in rows if r["event"] == "outcome"][0]
    assert out["first_partial_ms"] is None and out["problem_id"] == p["problem_id"]  # unknown is None, not 0
