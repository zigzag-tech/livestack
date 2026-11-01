"""Cold against warm, priced by when the answer is finished.

A speed preference used to mean "the fastest WARM unit": a cold unit competed
only when nothing anywhere was warm. So a fast host that had unloaded its model
lost to a slow host that kept one, however long the answer. `rank` now prices
each target as load (cold only) + first token + tokens / decode rate, from
MEASURED figures, and lets a cold target in when it beats every warm one.

Pinned here: the measured load time (`LoadTimes`) and its inventory spelling,
the rule in both directions, the silence cases that keep the old rule, and the
receipt that says what the caller is paying for.
"""
from livestack_node.demand_log import LoadTimes
from livestack_node.fleet_rank import finish_estimate, rank
from selection import unit_inventory

NOW = 1788600000.0
DECODE_MAX = [{"metric": "llm.decode_tok_s", "direction": "max"}]
NEED = {"class": "llm", "params_b>=": 100}
WHITBY = "http://100.64.0.27:8188"
TOWER = "http://100.64.0.18:8188"


def _obs(value, n=5):
    return {"value": value, "sample_count": n, "measured_at": NOW,
            "ttl_s": 86400.0, "model_revision": "measured:test"}


def _node(base, resident, decode=None, load=None, first_ms=None, probe_ms=2.0):
    llm = {}
    if decode is not None:
        llm["decode_tok_s"] = _obs(decode)
    if load is not None:
        llm["load_s"] = _obs(load)
    if first_ms is not None:
        llm["first_token_ms"] = _obs(first_ms)
    return {"peer": f"{base}/livestack", "state": "fresh", "ready": True,
            "kinds": ["llm"], "probe_ms": probe_ms,
            "units": [{"kind": "flash_next", "resident": resident,
                       "attributes": {"class": "llm", "params_b": 125}}],
            "inventory": {"units": {"flash_next": {"llm": llm}}}}


def _view(*nodes):
    return {"generated_at": NOW,
            "hosts": {n["peer"].split("//")[1].split(":")[0]: {"nodes": [n]} for n in nodes}}


def _first(view, tokens, prefer=DECODE_MAX):
    r = rank(view, "llm", now=NOW, require=NEED, prefer=prefer, expected_tokens=tokens)
    return r, r["targets"][0]["target_id"]


# -- the rule ------------------------------------------------------------------

def test_a_long_answer_pays_for_a_load_on_the_faster_host():
    # 4096 tok: warm tower 4096/41 = 100 s; cold whitby 12 + 4096/116 = 47 s.
    view = _view(_node(TOWER, True, decode=41), _node(WHITBY, False, decode=116, load=12))
    r, first = _first(view, 4096)
    assert first == WHITBY
    assert "its load pays for itself" in r["targets"][0]["reason"]
    assert r["targets"][0]["finish_s"] == 47.3
    assert r["targets"][1]["target_id"] == TOWER


def test_a_short_answer_stays_on_the_warm_host():
    # 300 tok: warm 7.3 s; cold 12 + 2.6 = 14.6 s. The load does not pay.
    view = _view(_node(TOWER, True, decode=41), _node(WHITBY, False, decode=116, load=12))
    r, first = _first(view, 300)
    assert first == TOWER
    held = next(c for c in r["candidates"] if c.target_id == WHITBY)
    assert held.outcome == "filtered"
    assert "finish≈14.6s with its 12.0s load, not sooner than warm 7.3s" in held.reason


def test_a_warm_host_loses_only_to_a_cold_one_that_finishes_sooner():
    # Cold tower 1 + 4096/300 = 14.7 s against warm whitby 35.3 s: the cold
    # one wins because it IS sooner, load included. A 60 s load and it is not.
    view = _view(_node(WHITBY, True, decode=116), _node(TOWER, False, decode=300, load=1))
    _, first = _first(view, 4096)
    assert first == TOWER
    view = _view(_node(WHITBY, True, decode=116), _node(TOWER, False, decode=300, load=60))
    _, first = _first(view, 4096)
    assert first == WHITBY


def test_both_cold_go_to_the_earliest_finish_not_the_fastest_decode():
    # Fast decode, slow load against slower decode, quick load, for 512 tok:
    # 120 + 4.4 = 124 s against 10 + 6.4 = 16 s.
    view = _view(_node(WHITBY, False, decode=116, load=120), _node(TOWER, False, decode=80, load=10))
    _, first = _first(view, 512)
    assert first == TOWER


# -- silence keeps the old rule ------------------------------------------------

def test_an_unmeasured_load_is_not_priced_and_stays_held_back():
    view = _view(_node(TOWER, True, decode=41), _node(WHITBY, False, decode=116))
    r, first = _first(view, 8192)
    assert first == TOWER
    held = next(c for c in r["candidates"] if c.target_id == WHITBY)
    assert "unpriced" in held.reason


def test_an_unpriced_warm_target_keeps_warm_first():
    # The tower publishes no speed: "faster than something we cannot price" is
    # not a finding, so the warm tower stands.
    view = _view(_node(TOWER, True), _node(WHITBY, False, decode=116, load=1))
    _, first = _first(view, 8192)
    assert first == TOWER


def test_without_a_speed_preference_nothing_changes():
    view = _view(_node(TOWER, True, decode=41), _node(WHITBY, False, decode=116, load=1))
    r = rank(view, "llm", now=NOW, require=NEED, expected_tokens=8192)
    assert r["targets"][0]["target_id"] == TOWER and len(r["targets"]) == 1
    r = rank(view, "llm", now=NOW, require=NEED, prefer=DECODE_MAX)   # no token count
    assert r["targets"][0]["target_id"] == TOWER and len(r["targets"]) == 1


def test_first_token_counts_when_measured():
    est = finish_estimate({"llm": {"decode_tok_s": _obs(100.0), "first_token_ms": _obs(2000.0),
                                   "load_s": _obs(10.0)}}, 100, True, NOW)
    assert est == {"finish_s": 13.0, "load_s": 10.0, "decode_tok_s": 100.0}
    assert finish_estimate({"llm": {"decode_tok_s": _obs(100.0)}}, 100, False, NOW)["finish_s"] == 1.0
    assert finish_estimate({"llm": {"decode_tok_s": _obs(100.0)}}, 100, True, NOW) is None


# -- the evidence --------------------------------------------------------------

def test_load_times_survive_a_restart_and_publish_their_median(tmp_path):
    path = str(tmp_path / "loads.jsonl")
    lt = LoadTimes(path)
    assert lt.summary("flash_next") == {}
    for s in (12.0, 60.0, 14.0):
        lt.observe("flash_next", s, ts=NOW)
    lt.observe("other", 5.0, ts=NOW)
    again = LoadTimes(path)
    assert again.summary("flash_next") == {"load_s": 14.0, "load_s_samples": 3,
                                           "load_measured_at": NOW}


def test_load_times_stay_bounded(tmp_path):
    lt = LoadTimes(str(tmp_path / "loads.jsonl"), window=10, keep=5)
    for i in range(30):
        lt.observe("u", float(i + 1), ts=NOW + i)
    lines = (tmp_path / "loads.jsonl").read_text().splitlines()
    assert len(lines) <= 5 * 4 + 1
    assert lt.summary("u")["load_s_samples"] <= 10


def test_inventory_publishes_a_measured_load_over_the_declared_one():
    inv = unit_inventory({"params_b": 125}, {"load_s": 14.0, "load_s_samples": 3,
                                             "load_measured_at": NOW, "reload_cost": 120})
    assert inv["llm"]["load_s"]["value"] == 14.0
    assert inv["llm"]["load_s"]["sample_count"] == 3
    inv = unit_inventory({"params_b": 125}, {"reload_cost": 120})
    assert inv["llm"]["load_s"]["value"] == 120.0
    assert inv["llm"]["load_s"]["model_revision"] == "declared"
    assert "load_s" not in unit_inventory({"params_b": 125}, {}).get("llm", {})
