"""client.admit carries the caller's identity the way the broker reads it:
`owner` in the body (for the credential to resolve against), `owner_asserted`
marking a hub-vouched owner, and the token on the Authorization header —
never in the body, which is logged, proxied and pasted into bug reports.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import livestack_node.client as client_module
from livestack_node.client import LeaseLost, admit, exclusive_lease


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


def _fake_dial(monkeypatch, handler):
    calls = []

    def dial(target, method, path, headers=None, body=None, timeout=None):
        call = {
            "target": target,
            "method": method,
            "path": path,
            "headers": headers or {},
            "body": json.loads(body or b"{}"),
            "timeout": timeout,
        }
        calls.append(call)
        result = handler(call)
        if isinstance(result, BaseException):
            raise result
        return 200, {}, json.dumps(result).encode()

    monkeypatch.setattr(client_module.transport, "dial", dial)
    return calls


def _exclusive_grant(call):
    return {"granted": True, "device_id": call["body"]["selector"]["device_id"],
            "lease_id": "lease/one"}


def test_exclusive_admission_sends_selector_limits_and_request_id(monkeypatch):
    calls = _fake_dial(monkeypatch, _exclusive_grant)
    res = admit("llm", owner_id="owner-a", exclusive_device=True,
                device_id="host/gpu0", min_free_bytes=123, wait_timeout_s=17,
                request_id="request-a", brokers=["http://broker"])
    assert res["lease_id"] == "lease/one"
    assert calls == [{
        "target": "http://broker",
        "method": "POST",
        "path": "/admit",
        "headers": {"Content-Type": "application/json"},
        "body": {
            "kind": "llm",
            "owner": "owner-a",
            "exclusive_device": True,
            "selector": {"device_id": "host/gpu0"},
            "min_free_bytes": 123,
            "wait_timeout_s": 17,
            "id": "request-a",
        },
        "timeout": 240.0,
    }]


def test_exclusive_admission_requires_a_device_and_no_owner_lease(monkeypatch):
    calls = _fake_dial(monkeypatch, _exclusive_grant)
    with pytest.raises(ValueError, match="requires device_id"):
        admit("llm", exclusive_device=True, brokers=["http://broker"])
    with pytest.raises(ValueError, match="mutually exclusive"):
        admit("llm", exclusive_device=True, device_id="host/gpu0",
              device_lease_id="owner-lease", brokers=["http://broker"])
    assert calls == []


@pytest.mark.parametrize("response", [
    {"granted": True, "device_id": None, "lease_id": "lease-a"},
    {"granted": True, "device_id": "host/gpu0", "lease_id": "lease-a",
     "degraded": "broker unavailable"},
    {"granted": True, "device_id": "host/gpu0"},
])
def test_exclusive_admission_rejects_non_authoritative_grants(monkeypatch, response):
    _fake_dial(monkeypatch, lambda call: response)
    with pytest.raises(LeaseLost):
        admit("llm", exclusive_device=True, device_id="host/gpu0",
              brokers=["http://broker"])


def test_owner_device_lease_is_forwarded_and_cannot_degrade(monkeypatch):
    calls = _fake_dial(monkeypatch, lambda call: {
        "granted": True, "device_id": "host/gpu0"})
    admit("llm", device_lease_id="owner/lease", brokers=["http://broker"])
    assert calls[0]["body"] == {
        "kind": "llm", "owner": "node", "device_lease_id": "owner/lease"}

    _fake_dial(monkeypatch, lambda call: {
        "granted": True, "device_id": None, "degraded": "no broker"})
    with pytest.raises(LeaseLost, match="degraded"):
        admit("llm", device_lease_id="owner/lease", brokers=["http://broker"])


def test_multiple_owner_device_leases_are_forwarded_without_degrading(monkeypatch):
    calls = _fake_dial(monkeypatch, lambda call: {
        "granted": True, "device_id": "host/gpu0"})
    admit("imagegen", device_lease_ids=["owner/lease0", "owner/lease1"],
          brokers=["http://broker"])
    assert calls[0]["body"] == {
        "kind": "imagegen", "owner": "node",
        "device_lease_ids": ["owner/lease0", "owner/lease1"]}
    with pytest.raises(ValueError, match="mutually exclusive"):
        admit("imagegen", device_lease_id="owner/lease0",
              device_lease_ids=["owner/lease0"], brokers=["http://broker"])


def test_exclusive_lease_heartbeats_and_releases_on_granting_broker(monkeypatch):
    heartbeat_seen = threading.Event()

    def handler(call):
        if call["path"] == "/admit":
            return {"granted": True, "device_id": "host/gpu0",
                    "lease_id": "lease/a b"}
        if call["path"] == "/lease/lease%2Fa%20b/heartbeat":
            heartbeat_seen.set()
            return {"ok": True}
        if call["path"] == "/lease/lease%2Fa%20b/release":
            return {"ok": True}
        raise AssertionError(call)

    calls = _fake_dial(monkeypatch, handler)
    with exclusive_lease("llm", device_id="host/gpu0", min_free_bytes=0, owner_id="owner-a",
                         token="secret", brokers=["http://broker"], ttl_seconds=17,
                         wait_timeout_s=3, heartbeat_interval=0.01) as granted:
        assert granted["lease_id"] == "lease/a b"
        assert heartbeat_seen.wait(1)

    paths = [call["path"] for call in calls]
    assert paths.count("/admit") == 1
    assert "/lease/lease%2Fa%20b/heartbeat" in paths
    assert paths[-1] == "/lease/lease%2Fa%20b/release"
    # The HTTP call outlives the broker's own drain window by a margin.
    assert calls[0]["timeout"] == 3 + 30
    assert "wait_s" not in calls[0]["body"]
    heartbeat = calls[paths.index("/lease/lease%2Fa%20b/heartbeat")]
    assert heartbeat["body"] == {"ttl_seconds": 17}
    assert all(call["headers"].get("Authorization") == "Bearer secret"
               for call in calls)


def test_heartbeat_rejection_surfaces_and_still_releases(monkeypatch):
    heartbeat_seen = threading.Event()

    def handler(call):
        if call["path"] == "/admit":
            return _exclusive_grant(call)
        if call["path"].endswith("/heartbeat"):
            heartbeat_seen.set()
            return {"ok": False, "reason": "expired"}
        return {"ok": False, "reason": "already expired"}

    calls = _fake_dial(monkeypatch, handler)
    with pytest.raises(LeaseLost, match="heartbeat rejected"):
        with exclusive_lease("llm", device_id="host/gpu0", min_free_bytes=0,
                             brokers=["http://broker"], heartbeat_interval=0.01):
            assert heartbeat_seen.wait(1)
    assert calls[-1]["path"].endswith("/release")


def test_heartbeat_transport_failure_surfaces(monkeypatch):
    heartbeat_seen = threading.Event()

    def handler(call):
        if call["path"] == "/admit":
            return _exclusive_grant(call)
        if call["path"].endswith("/heartbeat"):
            heartbeat_seen.set()
            return OSError("offline")
        return {"ok": True}

    calls = _fake_dial(monkeypatch, handler)
    with pytest.raises(LeaseLost, match="heartbeat failed"):
        with exclusive_lease("llm", device_id="host/gpu0", min_free_bytes=0,
                             brokers=["http://broker"], heartbeat_interval=0.01):
            assert heartbeat_seen.wait(1)
    assert calls[-1]["path"].endswith("/release")


def test_exclusive_lease_releases_when_the_body_raises(monkeypatch):
    def handler(call):
        if call["path"] == "/admit":
            return _exclusive_grant(call)
        return {"ok": True}

    calls = _fake_dial(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="work failed"):
        with exclusive_lease("llm", device_id="host/gpu0", min_free_bytes=0,
                             brokers=["http://broker"]):
            raise RuntimeError("work failed")
    assert [call["path"] for call in calls] == [
        "/admit", "/lease/lease%2Fone/release"]


def test_expired_release_is_not_an_error(monkeypatch):
    monkeypatch.setattr(client_module, "_admit", lambda *args, **kwargs: (
        {"granted": True, "device_id": "host/gpu0", "lease_id": "lease/one"},
        "http://broker"))
    monkeypatch.setattr(client_module, "_post", lambda *args, **kwargs: {
        "ok": False, "reason": "already expired"})

    with exclusive_lease("llm", device_id="host/gpu0", min_free_bytes=0, brokers=["http://broker"]):
        pass


def test_exclusive_acquisition_refusal_is_never_swallowed(monkeypatch):
    calls = _fake_dial(monkeypatch, lambda call: {
        "granted": False, "reason": "device busy"})
    with pytest.raises(LeaseLost, match="device busy"):
        with exclusive_lease("llm", device_id="host/gpu0", min_free_bytes=0,
                             brokers=["http://broker"]):
            pytest.fail("a refused lease must not enter the context")
    assert [call["path"] for call in calls] == ["/admit"]
