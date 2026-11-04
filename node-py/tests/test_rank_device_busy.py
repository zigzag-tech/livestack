"""A node whose device an exclusive lease holds is not a cold candidate.

`ready:false` alone means "cold" to the ranker (it loads on demand), which kept a
leased node eligible as the fallback when it was the only one (xc-tower-ubuntu
polyasr under a FLUX lease, 2026-11-04). `device_busy` says it cannot load."""
from livestack_node.fleet_rank import rank


def node(peer, ready=True, busy=None):
    n = {"peer": peer, "state": "fresh", "ready": ready, "kinds": ["polyasr"],
         "detail": "x", "probe_ms": 10.0, "load": {"in_flight": 0},
         "units": [{"kind": "asr", "resident": True}]}
    if busy:
        n["device_busy"] = busy
    return n


def view(*nodes):
    return {"generated_at": 1000.0, "hosts": {f"h{i}": {"nodes": [n]} for i, n in enumerate(nodes)}}


def test_a_leased_node_alone_is_filtered_with_its_reason_not_chosen_cold():
    r = rank(view(node("http://a/livestack", ready=False, busy="device held exclusively by harmony-image")),
             "polyasr", now=1000.0)
    assert r["chosen"] is None
    row = r["candidates"][0]
    assert row.outcome == "filtered" and "device busy" in row.reason and "harmony-image" in row.reason


def test_without_the_flag_an_unready_node_is_still_the_cold_fallback():
    r = rank(view(node("http://a/livestack", ready=False)), "polyasr", now=1000.0)
    assert r["chosen"] == "http://a"


def test_a_ready_peer_wins_over_a_leased_one():
    r = rank(view(node("http://a/livestack", ready=False, busy="held"), node("http://b/livestack")),
             "polyasr", now=1000.0)
    assert r["chosen"] == "http://b"
