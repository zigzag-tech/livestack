"""Measured speed as placement evidence: a request that states how fast it
needs an answer goes where that unit has empirically been fastest.

Three layers, each pinned here:

* MEASUREMENT (`UsageTail.speed`, `SpeedStats`): the engine's own decode rate
  when it reports one, a stream-timed rate otherwise, nothing for a body that
  arrived all at once; solo and burst samples kept apart, medians, real counts.
* WARMTH MEETS THE REQUIREMENT (`warm_for(require=)`): a resident unit that
  cannot serve the request does not make its node warm for it.
* PER-UNIT EVIDENCE (`rank` over `inventory["units"]`): `prefer` reads the
  evidence of the unit that would serve, not one number for the whole node.
"""
import json

from livestack_node.demand_log import SpeedStats, UsageTail
from livestack_node.fleet_rank import rank, warm_for

DECODE_MAX = [{"metric": "llm.decode_tok_s", "direction": "max"}]
LONG = {"class": "llm", "context_len>=": 131072}


# -- measurement ---------------------------------------------------------------

def _sse(obj):
    return b"data: " + json.dumps(obj).encode() + b"\n\n"


def test_a_stream_is_timed_from_its_first_text_to_its_last_chunk():
    t = UsageTail()
    t.feed(_sse({"choices": [{"delta": {"role": "assistant"}}]}), now=100.0)
    t.feed(_sse({"choices": [{"delta": {"content": "Hel"}}]}), now=100.5)
    t.feed(_sse({"choices": [{"delta": {"content": "lo"}}]}), now=101.0)
    t.feed(_sse({"choices": [], "usage": {"prompt_tokens": 9,
                                          "completion_tokens": 21}}), now=101.5)
    got = t.speed(sent_at=100.2)
    assert got["speed_source"] == "stream-timed"
    assert got["decode_tok_s"] == 20.0            # 20 tokens after the first, in 1.0 s
    assert got["first_token_ms"] == 300.0         # from reaching the engine, not the queue


def test_the_engines_own_rate_wins_over_the_proxys_clock():
    t = UsageTail()
    t.feed(_sse({"choices": [{"delta": {"content": "x"}}]}), now=10.0)
    t.feed(_sse({"usage": {"completion_tokens": 50},
                 "timings": {"predicted_per_second": 118.4}}), now=11.0)
    assert t.speed()["decode_tok_s"] == 118.4
    assert t.speed()["speed_source"] == "engine-timings"


def test_a_body_that_arrived_at_once_has_no_decode_rate_and_no_first_token():
    t = UsageTail()
    t.feed(json.dumps({"choices": [{"message": {"content": "4"}}],
                       "usage": {"completion_tokens": 40}}).encode(), now=5.0)
    assert t.speed(sent_at=1.0) == {"decode_tok_s": None, "first_token_ms": None,
                                    "speed_source": None}


def test_too_few_tokens_is_not_a_rate():
    t = UsageTail()
    t.feed(_sse({"choices": [{"delta": {"content": "OK"}}]}), now=1.0)
    t.feed(_sse({"usage": {"completion_tokens": 2}}), now=1.01)
    assert t.speed()["decode_tok_s"] is None


def test_solo_and_burst_rates_are_different_numbers():
    s = SpeedStats()
    for v in (100.0, 104.0, 98.0):
        s.observe("flash_next", v, 400.0, in_flight=1)
    for v in (52.0, 50.0):
        s.observe("flash_next", v, 900.0, in_flight=2)
    got = s.summary("flash_next")
    assert got["cohort"] == "solo"
    assert got["decode_tok_s"] == 100.0 and got["decode_tok_s_samples"] == 3
    assert got["decode_tok_s_burst"] == 51.0 and got["decode_tok_s_burst_samples"] == 2
    assert got["first_token_ms"] == 400.0


def test_a_unit_that_never_ran_alone_says_so():
    s = SpeedStats()
    s.observe("u", 50.0, None, in_flight=3)
    assert s.summary("u")["cohort"] == "any"
    assert "first_token_ms" not in s.summary("u")
    assert s.summary("never") == {}


def test_a_restart_keeps_the_evidence_it_earned(tmp_path):
    log = tmp_path / "demand.jsonl"
    rows = [{"ts": 1.0, "unit": "flash_next", "outcome": "ok", "in_flight": 1,
             "decode_tok_s": 110.0, "first_token_ms": 350.0},
            {"ts": 2.0, "unit": "flash_next", "outcome": "refused", "decode_tok_s": 1.0},
            {"ts": 3.0, "unit": "flash_next", "outcome": "ok"}]
    log.write_text("".join(json.dumps(r) + "\n" for r in rows) + '{"trunc')
    s = SpeedStats()
    assert s.seed(str(log)) == 1
    assert s.summary("flash_next")["decode_tok_s"] == 110.0


# -- warmth must meet the requirement ------------------------------------------

def _unit(kind, resident, ctx, inventory_name=None, **attrs):
    return {"kind": kind, "resident": resident,
            "attributes": {"class": "llm", "context_len": ctx, **attrs}}


TOWER = {"peer": "http://100.64.0.18:8188/livestack", "state": "fresh", "ready": True,
         "kinds": ["llm"], "probe_ms": 2.0,
         "units": [_unit("llm_general", True, 24576, params_b=27),
                   _unit("flash_next", False, 131072, params_b=125)]}


def test_a_resident_unit_that_cannot_serve_the_request_is_not_warmth():
    assert warm_for(TOWER, "llm") is True
    assert warm_for(TOWER, "llm", LONG) is False
    assert warm_for(TOWER, "llm", {"class": "llm", "context_len>=": 16384}) is True


# -- per-unit evidence decides among nodes --------------------------------------

def _obs(value, n=5):
    return {"value": value, "sample_count": n, "measured_at": 1788600000.0,
            "ttl_s": 86400.0, "model_revision": "measured:test"}


def _whitby(resident):
    return {"peer": "http://100.64.0.27:8188/livestack", "state": "fresh", "ready": True,
            "kinds": ["llm"], "probe_ms": 40.0,
            "units": [_unit("flash_next", resident, 131072, params_b=125)],
            "inventory": {"units": {"flash_next": {"llm": {"decode_tok_s": _obs(118.0)}}}}}


def _view(*nodes):
    return {"generated_at": 1788600000.0,
            "hosts": {n["peer"].split("//")[1].split(":")[0]: {"nodes": [n]} for n in nodes}}


def test_measured_speed_outranks_silence_and_distance_when_the_caller_asks_for_it():
    # Both can serve 131K only by loading Flash-Next; the tower is closer, but
    # only whitby has evidence of how fast that unit runs.
    r = rank(_view(TOWER, _whitby(False)), "llm", now=1788600000.0,
             require=LONG, prefer=DECODE_MAX)
    assert [t["target_id"] for t in r["targets"]][0] == "http://100.64.0.27:8188"


def test_without_a_preference_distance_still_decides():
    r = rank(_view(TOWER, _whitby(False)), "llm", now=1788600000.0, require=LONG)
    assert [t["target_id"] for t in r["targets"]][0] == "http://100.64.0.18:8188"


def test_the_evidence_read_is_the_serving_units_not_a_faster_sibling():
    # The tower's 27B is fast, but it cannot serve 131K; its Flash-Next is slow.
    tower = dict(TOWER, inventory={"units": {
        "llm_general": {"llm": {"decode_tok_s": _obs(300.0)}},
        "flash_next": {"llm": {"decode_tok_s": _obs(60.0)}}}})
    r = rank(_view(tower, _whitby(False)), "llm", now=1788600000.0,
             require=LONG, prefer=DECODE_MAX)
    assert [t["target_id"] for t in r["targets"]][0] == "http://100.64.0.27:8188"
    # ...and for a short request the 27B's own evidence is what counts.
    r = rank(_view(tower, _whitby(False)), "llm", now=1788600000.0,
             require={"class": "llm"}, prefer=DECODE_MAX)
    assert [t["target_id"] for t in r["targets"]][0] == "http://100.64.0.18:8188"
