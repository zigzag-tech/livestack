"""client.admit carries the caller's identity the way the broker reads it:
`owner` in the body (for the credential to resolve against), `owner_asserted`
marking a hub-vouched owner, and the token on the Authorization header —
never in the body, which is logged, proxied and pasted into bug reports.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from livestack_node.client import admit


class _Capture(BaseHTTPRequestHandler):
    seen = []  # class-level: one list per server process

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        type(self).seen.append({
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": body,
        })
        payload = json.dumps({"granted": True, "device_id": "h/gpu0"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def _server():
    _Capture.seen = []
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Capture)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_the_token_rides_the_authorization_header_and_the_body_names_the_owner():
    srv, base = _server()
    try:
        res = admit("llm", owner_id="attune:acct_a", token="t" * 40,
                    owner_asserted=True, brokers=[base])
        assert res.get("granted") is True
        req = _Capture.seen[0]
        assert req["path"] == "/admit"
        assert req["authorization"] == f"Bearer {'t' * 40}"
        assert req["body"]["owner"] == "attune:acct_a"
        assert req["body"]["owner_asserted"] is True
        assert "t" * 40 not in json.dumps(req["body"]), \
            "a credential must never ride the body"
    finally:
        srv.shutdown()


def test_without_a_token_no_header_is_sent_and_behaviour_is_todays():
    """Existing callers against a broker with no principals: the request is
    byte-for-byte what it was before the token existed."""
    srv, base = _server()
    try:
        admit("llm", owner_id="harmony-llm:test", brokers=[base])
        req = _Capture.seen[0]
        assert req["authorization"] is None
        assert req["body"] == {"kind": "llm", "owner": "harmony-llm:test"}
    finally:
        srv.shutdown()


def test_owner_asserted_is_omitted_unless_true():
    """The flag is only sent when set, so an unmarked admission reads exactly
    as the millions of requests that predate the field."""
    srv, base = _server()
    try:
        admit("llm", owner_id="harmony-llm:test", token="t" * 40, brokers=[base])
        assert "owner_asserted" not in _Capture.seen[0]["body"]
    finally:
        srv.shutdown()


def test_requires_rides_along_with_the_credential():
    srv, base = _server()
    try:
        admit("", requires={"class": "llm", "params_b>=": 20},
              owner_id="benchday:acct_b", token="b" * 40, brokers=[base])
        req = _Capture.seen[0]
        assert req["body"]["requires"] == {"class": "llm", "params_b>=": 20}
        assert req["body"]["owner"] == "benchday:acct_b"
        assert req["authorization"] == f"Bearer {'b' * 40}"
    finally:
        srv.shutdown()
