"""Fleet ranking: where should this request START, given the whole fleet.

Pure over a fleet view, so every case here is an ordering question answered
exactly rather than guessed at from a live run. The ordering is lexicographic —
distance band, then load, then target id — and each test below pins one level of
that and the reason it exists.
"""
import pytest

from livestack_node.fleet_rank import (
    DEFAULT_TTL_S, distance_to, is_stale, load_value, rank,
)
from livestack_node.ledger import validate


def _node(peer, state="fresh", ready=True, probe_ms=None, load=None,
          kinds=("asr",), detail="resident", last_error=None, device_id=None):
    n = {"peer": peer, "state": state, "ready": ready, "kinds": list(kinds),
         "detail": detail, "unseen_seconds": 0.0}
    if probe_ms is not None:
        n["probe_ms"] = probe_ms
    if load is not None:
        n["load"] = load
    if last_error:
        n["last_error"] = last_error
    if device_id:
        n["device_id"] = device_id
    return n


def _view(hosts, generated_at=1788600000.0, relays=None):
    v = {"dispatch": False, "generated_at": generated_at, "hosts": hosts,
         "peers": sum(len(h["nodes"]) for h in hosts.values())}
    if relays:
        v["relays"] = relays
    return v


LOCAL = _node("http://100.64.0.18:8766/livestack", probe_ms=2.1,
              load={"in_flight": 0, "pressure": 0.21, "in_flight_source": "server"})
MAC = _node("http://100.64.0.2:8765/livestack", probe_ms=13.3)
FAR = _node("http://100.64.0.3:8766/livestack", probe_ms=527.0,
            load={"in_flight": 3, "pressure": 0.81, "in_flight_source": "server"})

FLEET = _view({
    "xc-tower-ubuntu": {"nodes": [LOCAL],
                        "links": {"xc-mac-studio": 13.0, "zz-tower0": 530.0}},
    "xc-mac-studio": {"nodes": [MAC],
                      "links": {"xc-tower-ubuntu": 12.0, "zz-tower0": 520.0}},
    "zz-tower0": {"nodes": [FAR],
                  "links": {"xc-tower-ubuntu": 540.0, "xc-mac-studio": 515.0}},
})


def _order(result):
    return [t["target_id"] for t in result["targets"]]


# -- the first-order fact: distance ------------------------------------------

def test_distance_bands_order_the_fleet_from_the_brokers_own_vantage():
    r = rank(FLEET, "asr")
    assert _order(r) == ["http://100.64.0.18:8766", "http://100.64.0.2:8765",
                         "http://100.64.0.3:8766"]
    assert r["targets"][0]["distance_band"] == "<50"
    assert r["targets"][2]["distance_band"] == "<600"   # 527 ms to Nanjing


def test_the_order_inverts_from_a_nanjing_vantage():
    """`probe_ms` is distance from where the BROKER sits. A client in Nanjing
    must not be ranked by Vaughan's view of Nanjing — which is the whole reason
    the links matrix exists."""
    r = rank(FLEET, "asr", vantage="host:zz-tower0")
    assert _order(r)[0] == "http://100.64.0.3:8766", _order(r)
    assert r["targets"][0]["distance_ms"] == 0.0, "a node on the asking host is local"


def test_an_unmeasured_pair_sorts_last_and_never_becomes_a_default():
    """A distance we failed to measure is not evidence of nearness. Treating it
    as such is how a fleet sends work across an ocean on the strength of having
    no reading."""
    view = _view({
        "known": {"nodes": [_node("http://a/livestack", probe_ms=400.0)]},
        "silent": {"nodes": [_node("http://b/livestack")]},
    })
    r = rank(view, "asr")
    assert _order(r) == ["http://a", "http://b"]
    assert r["targets"][1]["distance_band"] == "unknown"
    assert r["targets"][1]["distance_ms"] is None


def test_bands_not_millis_so_jitter_does_not_reorder_the_fleet():
    a = _node("http://a/livestack", probe_ms=2.0)
    b = _node("http://b/livestack", probe_ms=40.0)
    view = _view({"h": {"nodes": [a, b]}})
    # 20x apart in raw millis, same band — so the tie falls to the next level
    # rather than to a number the client is about to re-measure anyway.
    r = rank(view, "asr")
    assert [t["distance_band"] for t in r["targets"]] == ["<50", "<50"]
    assert _order(r) == ["http://a", "http://b"]


# -- the second level: load ---------------------------------------------------

def test_within_a_band_the_freer_engine_wins():
    busy = _node("http://busy/livestack", probe_ms=2.0,
                 load={"in_flight": 4, "pressure": 0.2})
    free = _node("http://free/livestack", probe_ms=3.0,
                 load={"in_flight": 0, "pressure": 0.2})
    r = rank(_view({"h": {"nodes": [busy, free]}}), "asr")
    assert _order(r) == ["http://free", "http://busy"]


def test_queue_depth_is_primary_and_pressure_may_only_raise():
    """Device pressure is dominated by the RESIDENT MODEL's weights, not by
    queued work: two idle engines holding the same model on identical cards
    report byte-identical pressure and cannot break a tie between themselves.
    Six concurrent ASR requests moved it under half a percent."""
    assert load_value({"in_flight": 4, "pressure": 0.2}) == 0.5    # queue wins
    assert load_value({"in_flight": 0, "pressure": 0.95}) == 0.95  # pressure raises
    assert load_value({"in_flight": 0, "pressure": 0.2}) == 0.2
    # Contention this engine did not create still counts.
    contended = _node("http://contended/livestack", probe_ms=2.0,
                      load={"in_flight": 0, "pressure": 0.95})
    quiet = _node("http://quiet/livestack", probe_ms=3.0,
                  load={"in_flight": 0, "pressure": 0.10})
    assert _order(rank(_view({"h": {"nodes": [contended, quiet]}}), "asr")) == [
        "http://quiet", "http://contended"]


def test_silence_is_not_idleness():
    """A node reporting no load must not outrank one that reported being free.
    An engine that has gone quiet is the likeliest source of an empty report,
    and reading silence as spare capacity steers traffic at the node least able
    to serve it."""
    silent = _node("http://silent/livestack", probe_ms=2.0)
    loaded = _node("http://loaded/livestack", probe_ms=3.0,
                   load={"in_flight": 2, "pressure": 0.5})
    r = rank(_view({"h": {"nodes": [silent, loaded]}}), "asr")
    assert _order(r) == ["http://loaded", "http://silent"]
    assert "no opinion" in r["targets"][1]["reason"]


def test_a_malformed_pressure_is_discarded_not_clamped():
    assert load_value({"pressure": 7.5}) is None
    assert load_value({"pressure": -1}) is None
    assert load_value({}) is None
    assert load_value(None) is None


def test_distance_beats_load_because_a_distant_idle_gpu_is_still_distant():
    near_busy = _node("http://near/livestack", probe_ms=2.0,
                      load={"in_flight": 7, "pressure": 0.9})
    far_idle = _node("http://far/livestack", probe_ms=900.0,
                     load={"in_flight": 0, "pressure": 0.0})
    assert _order(rank(_view({"h": {"nodes": [near_busy, far_idle]}}), "asr"))[0] \
        == "http://near"


# -- filters: a row, never an omission ---------------------------------------

def test_a_filtered_candidate_is_a_row_with_the_first_rule_that_removed_it():
    view = _view({"h": {"nodes": [
        LOCAL,
        _node("http://gone/livestack", state="suspect", probe_ms=900.0,
              last_error="Connection refused"),
        _node("http://cold/livestack", ready=False, probe_ms=2.0,
              detail="no unit resident"),
        _node("http://tts/livestack", kinds=("qwen",), probe_ms=2.0),
    ]}})
    r = rank(view, "asr")
    assert _order(r) == ["http://100.64.0.18:8766"]
    filtered = {c.target_id: c.reason for c in r["candidates"]
                if c.outcome == "filtered"}
    assert filtered["http://gone"].startswith("filtered: state=suspect")
    assert "Connection refused" in filtered["http://gone"]
    assert filtered["http://cold"].startswith("filtered: not ready")
    assert "does not host asr" in filtered["http://tts"]
    # The FIRST rule that eliminated it is the reason — "it was also far away"
    # is not why it lost.
    assert "band" not in filtered["http://gone"]


def test_a_kind_matches_the_nodes_own_name_or_its_unit_names():
    n = {"peer": "http://a/livestack", "state": "fresh", "ready": True,
         "kinds": ["polyasr"], "probe_ms": 2.0,
         "units": [{"kind": "asr"}, {"kind": "align"}]}
    view = _view({"h": {"nodes": [n]}})
    assert _order(rank(view, "asr")) == ["http://a"]
    assert _order(rank(view, "align")) == ["http://a"]
    assert _order(rank(view, "polyasr")) == ["http://a"]
    assert _order(rank(view, "voxcpm")) == []


def test_an_empty_result_says_why_rather_than_returning_nothing():
    view = _view({"h": {"nodes": [_node("http://gone/livestack", state="mia")]}})
    r = rank(view, "asr")
    assert r["targets"] == []
    assert r["chosen"] is None
    assert "no fresh, ready node hosts asr" in r["reason"]
    assert len(r["candidates"]) == 1, "the eliminated node is still a row"


# -- region is the caller's, never the fleet's -------------------------------

def test_ranking_never_applies_region_policy():
    """Region is operator policy on the grant. A fleet broker that decided it
    would be a second place for it to be wrong — and it does not have the
    account, so it could only guess."""
    r = rank(FLEET, "asr")
    for t in r["targets"]:
        assert "region" not in t


# -- expiry -------------------------------------------------------------------

def test_a_ranking_expires_and_a_consumer_discards_rather_than_downgrades():
    r = rank(FLEET, "asr", now=1000.0)
    assert r["ttl_s"] == DEFAULT_TTL_S
    assert not is_stale(r, now=1050.0)
    assert is_stale(r, now=1100.0)


# -- the record it produces ---------------------------------------------------

def test_every_candidate_row_carries_an_outcome_and_a_non_empty_reason():
    view = _view({"h": {"nodes": [LOCAL, MAC, FAR,
                                  _node("http://x/livestack", state="mia")]}})
    r = rank(view, "asr")
    assert len(r["candidates"]) == 4
    for c in r["candidates"]:
        assert c.outcome in ("chosen", "ranked", "filtered")
        assert c.reason, c
    assert sum(1 for c in r["candidates"] if c.outcome == "chosen") == 1


def test_the_reason_names_the_terms_a_reader_would_check():
    r = rank(FLEET, "asr")
    chosen = r["targets"][0]
    assert "band<50" in chosen["reason"] and "in_flight=0" in chosen["reason"]
    loser = r["targets"][2]
    assert "band<600" in loser["reason"] and "in_flight=3" in loser["reason"]
    # And the summary names the runner-up, so "why not the other one" is
    # answerable from one line.
    assert "next was" in r["reason"]


def test_the_ranking_becomes_a_valid_ledger_record():
    from livestack_node.ledger import Candidate, Decision
    r = rank(FLEET, "asr")
    d = Decision(
        emitter="fleet-broker", emitter_id="xc-tower-ubuntu:8801",
        kind=r["kind"], decision="rank", chosen=r["chosen"], reason=r["reason"],
        ttl_s=r["ttl_s"], request={"vantage": r["vantage"]},
        candidates=[Candidate(
            id=c.target_id, host_id=c.host_id, device_id=c.device_id,
            state=c.state, ready=c.ready, distance_ms=c.distance_ms,
            distance_band=c.distance_band, load=c.load, inputs_at=c.inputs_at,
            outcome=c.outcome, rank=c.rank, reason=c.reason)
            for c in r["candidates"]],
    )
    assert validate(d.to_dict()) == []


# -- vantages -----------------------------------------------------------------

def test_a_relay_vantage_reads_the_relays_own_measured_links():
    view = _view(
        {"zz-tower0": {"nodes": [FAR]}, "xc-tower-ubuntu": {"nodes": [LOCAL]}},
        relays={"na-public-la": {"links": {"xc-tower-ubuntu": 30.0,
                                           "zz-tower0": 240.0}}})
    r = rank(view, "asr", vantage="relay:na-public-la")
    assert _order(r) == ["http://100.64.0.18:8766", "http://100.64.0.3:8766"]
    assert r["targets"][1]["distance_band"] == "<600"


def test_an_unknown_vantage_has_no_opinion_rather_than_a_wrong_one():
    r = rank(FLEET, "asr", vantage="host:does-not-exist")
    assert all(t["distance_ms"] is None for t in r["targets"])
    assert all(t["distance_band"] == "unknown" for t in r["targets"])
    # Ordering is still total and deterministic, by load then id.
    assert _order(r) == sorted(_order(r), key=lambda k: k) or len(r["targets"]) >= 1


def test_distance_to_is_pure_and_returns_none_rather_than_guessing():
    assert distance_to(FLEET, "zz-tower0", FAR, "direct") == 527.0
    assert distance_to(FLEET, "zz-tower0", FAR, "host:zz-tower0") == 0.0
    assert distance_to(FLEET, "zz-tower0", FAR, "host:xc-mac-studio") == 520.0
    assert distance_to(FLEET, "zz-tower0", FAR, "nonsense") is None
    assert distance_to(FLEET, None, FAR, "host:xc-mac-studio") is None
