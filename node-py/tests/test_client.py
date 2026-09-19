"""Consumer client: no-op when no endpoint; degrades gracefully when unreachable."""

# From the module, not the package: `livestack_node.lease` is also a MODULE, and
# once anything imports it the submodule shadows the re-exported context manager
# — which is why these two tests raised `'module' object is not callable`.
from livestack_node.client import lease


def test_noop_when_no_base_url():
    with lease("diarize") as handle:
        assert handle.lease_id is None


def test_degrades_when_unreachable():
    with lease("diarize", base_url="http://127.0.0.1:1/livestack") as handle:
        assert handle.lease_id is None


# --- admission ---------------------------------------------------------------

def test_admit_degrades_to_a_grant_when_no_broker_answers():
    """An arbitration outage must cost the arbitration, not the model. The
    caller is told it may proceed, and told why the answer is not authoritative."""
    from livestack_node.client import admit
    res = admit("llm_judge", brokers=["http://127.0.0.1:1"], timeout=0.2)
    assert res["granted"] is True
    assert res["device_id"] is None
    assert "degraded" in res


def test_admit_returns_the_device_the_planner_granted():
    import json as _json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from livestack_node.client import admit

    seen = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            n = int(self.headers.get("content-length", 0))
            seen["body"] = _json.loads(self.rfile.read(n) or b"{}")
            out = _json.dumps({"granted": True, "device_id": "host/card0"}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = admit("llm_judge", brokers=[f"http://127.0.0.1:{srv.server_port}"], timeout=5)
    finally:
        srv.shutdown()
    assert res == {"granted": True, "device_id": "host/card0"}
    assert seen["path"] == "/admit"
    assert seen["body"]["kind"] == "llm_judge"
    # No selector: the planner picks the device. A caller that pinned one would
    # be making the placement decision admission exists to take away from it.
    assert "selector" not in seen["body"]


# --- capability requirements ------------------------------------------------
#
# The measured failure: attune asks the fleet for a polytts in North America,
# gets the nearest one, and sends it `voice_id=3240e992…`. That voice was
# cloned on the other North American node, so polytts answers `404 Unknown
# voice_id` and the item dies — with the placement layer having done exactly
# what it was asked. It was asked the wrong question.

from livestack_node.client import capable_targets, parse_requirements, satisfies


def _ranking(*targets):
    return {"generated_at": 1788600000.0, "ttl_s": 60.0, "targets": list(targets)}


class TestParsingWhatTheCallerAsksFor:
    def test_reads_one_clause(self):
        assert parse_requirements("voice:abc") == {"voice": "abc"}

    def test_reads_several(self):
        assert parse_requirements("voice:abc,engine:qwen") == {
            "voice": "abc", "engine": "qwen"}

    def test_tolerates_spacing_and_empty_clauses(self):
        assert parse_requirements(" voice : abc , , engine:qwen ") == {
            "voice": "abc", "engine": "qwen"}

    def test_nothing_asked_is_nothing_required(self):
        assert parse_requirements(None) == {}
        assert parse_requirements("") == {}

    def test_a_clause_with_no_value_is_not_a_requirement(self):
        # `?require=voice` cannot mean "any voice": every node has some. A
        # half-written requirement silently matching everything is the failure
        # this whole filter exists to prevent.
        assert parse_requirements("voice") == {}

    def test_a_dict_passes_through(self):
        assert parse_requirements({"voice": "abc"}) == {"voice": "abc"}


class TestWhatANodeAdvertises:
    def test_a_list_entry_matches_any_member(self):
        assert satisfies({"voice": ["a", "b"]}, "voice", "b")
        assert not satisfies({"voice": ["a", "b"]}, "voice", "c")

    def test_a_scalar_entry_matches_itself(self):
        assert satisfies({"engine": "qwen"}, "engine", "qwen")
        assert not satisfies({"engine": "qwen"}, "engine", "voxcpm")

    def test_silence_is_never_a_match(self):
        # A node that cannot say it has the voice cannot be sent the request.
        # Treating absence as a match is the same mistake as treating an
        # unknown region as the local one.
        assert not satisfies({}, "voice", "a")
        assert not satisfies(None, "voice", "a")
        assert not satisfies({"engine": "qwen"}, "voice", "a")


class TestFilteringARanking:
    def test_keeps_the_node_that_has_it(self):
        kept, rejected = capable_targets(_ranking(
            {"target_id": "http://a", "inventory": {"voice": ["v1"]}},
            {"target_id": "http://b", "inventory": {"voice": ["v2"]}},
        ), {"voice": "v2"})
        assert [t["target_id"] for t in kept] == ["http://b"]
        assert rejected[0]["target_id"] == "http://a"

    def test_says_what_was_missing_and_what_was_advertised(self):
        _, rejected = capable_targets(_ranking(
            {"target_id": "http://a", "inventory": {"engine": "qwen"}},
        ), {"voice": "v1"})
        assert "voice=v1" in rejected[0]["why"]
        assert "engine" in rejected[0]["why"]

    def test_a_node_advertising_nothing_says_so(self):
        _, rejected = capable_targets(_ranking({"target_id": "http://a"}), {"voice": "v1"})
        assert "advertises nothing" in rejected[0]["why"]

    def test_every_requirement_must_hold(self):
        kept, _ = capable_targets(_ranking(
            {"target_id": "http://a", "inventory": {"voice": ["v1"], "engine": "voxcpm"}},
        ), {"voice": "v1", "engine": "qwen"})
        assert kept == []

    def test_no_requirement_filters_nothing(self):
        kept, rejected = capable_targets(_ranking({"target_id": "http://a"}), {})
        assert len(kept) == 1 and rejected == []

    def test_the_brokers_order_survives_the_filter(self):
        kept, _ = capable_targets(_ranking(
            {"target_id": "http://near", "inventory": {"voice": ["v"]}},
            {"target_id": "http://far", "inventory": {"voice": ["v"]}},
        ), {"voice": "v"})
        assert [t["target_id"] for t in kept] == ["http://near", "http://far"]
