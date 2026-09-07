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
