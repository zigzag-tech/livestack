"""Ready is per-NODE; residency is per-UNIT. Conflating them hands out a node
that cannot serve what it was picked for.

Measured on xc-tower-ubuntu 2026-09-23: `xc-tower-ubuntu-gpu0` reported
`ready: true` with only a 0.6B embedder resident, advertised `kinds: ['llm']`,
sorted first, and had no vLLM of its own — so it won every `kind=llm` lookup and
proxied each request upstream through a serialising hop. One call answered in
0.9 s; twenty-five concurrent gave p50 7.5 s and blew 10 deadlines. Every check
from outside said healthy, because at concurrency 1 it was.
"""
from livestack_node.fleet_rank import rank, warm_for


def unit(kind, cls, resident):
    return {"kind": kind, "priority": 30, "residency": 2,
            "footprint": {"vram_bytes": 1}, "resident": resident, "busy": False,
            "attributes": {"class": cls}}


def node(peer, units, ready=True, state="fresh", probe=10.0):
    return {"peer": peer, "state": state, "ready": ready, "kinds": ["llm"],
            "detail": "serving", "unseen_seconds": 0.0, "probe_ms": probe,
            "device_id": "d", "units": units, "load": {"in_flight": 0}}


def view(*nodes):
    return {"generated_at": 1000.0,
            "hosts": {f"h{i}": {"nodes": [n]} for i, n in enumerate(nodes)}}


EMBED_ONLY = [unit("embed_multi", "embed", True), unit("llm_title", "llm", False)]
LLM_WARM = [unit("embed_multi", "embed", False), unit("llm_title", "llm", True)]


# --- the predicate -----------------------------------------------------------
def test_a_node_holding_only_an_embedder_is_not_warm_for_llm():
    assert warm_for(node("http://a", EMBED_ONLY), "llm") is False
    assert warm_for(node("http://a", EMBED_ONLY), "embed") is True


def test_a_node_holding_the_kind_is_warm_for_it():
    assert warm_for(node("http://b", LLM_WARM), "llm") is True


def test_a_node_that_publishes_no_class_is_UNKNOWN_not_cold():
    """A node predating per-unit attributes must behave exactly as before, or
    this change silently removes working capacity from every fleet."""
    bare = [{"kind": "llm_title", "resident": True}]
    assert warm_for(node("http://c", bare), "llm") is None
    assert warm_for(node("http://c", []), "llm") is None
    assert warm_for({"peer": "http://c"}, "llm") is None


# --- the ordering ------------------------------------------------------------
def test_the_llm_request_goes_to_the_node_that_holds_an_llm():
    """The whole point: equidistant, both `ready`, and only one can serve."""
    r = rank(view(node("http://embed-only", EMBED_ONLY, probe=5.0),
                  node("http://llm-warm", LLM_WARM, probe=40.0)), "llm", now=1000.0)
    assert r["chosen"] == "http://llm-warm", r["reason"]
    # And it says why the loser lost. Cold rows are not offered as `targets`
    # (that would be handing out the thing we just declined), so the reason
    # lands in the candidate set the ledger records.
    rows = {c.target_id: c for c in r["candidates"]}
    assert "no llm unit resident" in rows["http://embed-only"].reason
    assert "embed_multi" in rows["http://embed-only"].reason


def test_the_class_compared_against_is_the_NODE_kind_it_was_asked_for():
    """`rank`'s `kind` is the node-level kind a caller asks for (`llm`,
    `polyasr`), and a unit's `class` is what that unit IS. They meet here: a
    node advertising `kinds: ['llm']` is warm for that ask only while some
    llm-class unit is resident on it. Nothing asks `rank` for `embed` today —
    embedding demand arrives as a `require` clause against an llm node — so
    this is deliberately a test of the predicate, not of a second node kind.
    """
    assert warm_for(node("http://a", EMBED_ONLY), "llm") is False
    assert warm_for(node("http://a", LLM_WARM), "llm") is True


def test_a_cold_node_is_still_used_when_nothing_warm_exists():
    """`warm first, ALWAYS` — not `warm only`. Dropping the cold node outright
    is what made the first request after an idle eviction unroutable, so nothing
    could ever warm it again."""
    r = rank(view(node("http://embed-only", EMBED_ONLY)), "llm", now=1000.0)
    assert r["chosen"] == "http://embed-only"


def test_distance_still_decides_between_two_warm_nodes():
    """The correction is a warmth test, not a new ordering. Among nodes that can
    all serve the kind, the documented contract still holds."""
    r = rank(view(node("http://near", LLM_WARM, probe=5.0),
                  node("http://far", LLM_WARM, probe=900.0)), "llm", now=1000.0)
    assert r["chosen"] == "http://near", r["reason"]
