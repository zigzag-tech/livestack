"""Record/replay tests for the transport dial seam (tasks 4.1/4.2 of the
meshlink-transport-backbone change).

Two halves:

1. The default implementation, proven against a REAL loopback HTTP server —
   the positive control. A fake standing in for urllib would only prove the
   fake; these tests prove the moved urllib behavior (roundtrip, redirects,
   HTTPError on >= 400 with a readable error body, URLError on refusal).

2. A record/replay Recorder installed over `livestack_node.transport.dial` /
   `.dial_stream`, proving that every re-pointed call site actually routes
   through the seam — each test drives a real call-site function and asserts
   the recorded (target, method, path, headers, body) plus the replayed
   response propagating back through the site's own error handling.
"""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import time
import urllib.error
from email.message import Message
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from livestack_node import transport


# ---------------------------------------------------------------------------
# Positive control: default implementation against a real socket
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep pytest output readable
        pass

    def do_GET(self):
        if self.path == "/ping":
            self._send(200, b"pong")
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ping")
            self.end_headers()
        else:
            self._send(404, b"not here")

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        self._send(200, self.rfile.read(length))

    def _send(self, status, body):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def http_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_default_dial_roundtrips_get_and_post(http_server):
    status, headers, body = transport.dial(http_server, "GET", "/ping")
    assert (status, body) == (200, b"pong")
    assert int(headers.get("Content-Length")) == len(b"pong")

    status, _headers, body = transport.dial(
        http_server, "POST", "/echo",
        headers={"Content-Type": "application/json"}, body=b'{"a": 1}')
    assert (status, body) == (200, b'{"a": 1}')


def test_default_dial_follows_redirects(http_server):
    status, _headers, body = transport.dial(http_server, "GET", "/redirect")
    assert (status, body) == (200, b"pong")


def test_default_dial_raises_http_error_with_readable_body(http_server):
    with pytest.raises(urllib.error.HTTPError) as err:
        transport.dial(http_server, "GET", "/missing")
    assert err.value.code == 404
    assert err.value.read() == b"not here"


def test_default_dial_raises_url_error_on_refused():
    with pytest.raises(urllib.error.URLError):
        transport.dial("http://127.0.0.1:9", "GET", "/x", timeout=1)


def test_split_target_separates_base_and_query():
    assert transport.split_target("http://h:1/a/b?c=1") == ("http://h:1", "/a/b?c=1")
    assert transport.split_target("http://h:1") == ("http://h:1", "/")


# ---------------------------------------------------------------------------
# Record/replay fake for the seam
# ---------------------------------------------------------------------------

class ReplayResponse:
    """The dial_stream replay object: the surface streaming call sites use
    (.status, .headers.get(), .read(), .read1(), context manager)."""

    def __init__(self, status, headers, body: bytes):
        import io
        self.status = status
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self._stream = io.BytesIO(body)
        self.closed = False

    def read(self, n=-1):
        return self._stream.read(n)

    def read1(self, n=-1):
        return self._stream.read1(n)

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


class Recorder:
    """Records every seam call; replays queued responses in order. A queued
    exception is raised; a queued (status, headers, body) tuple is returned
    (wrapped in a ReplayResponse on the dial_stream path)."""

    def __init__(self):
        self.calls = []
        self._script = []

    def queue(self, *steps):
        self._script.extend(steps)

    def _record(self, target, method, path, headers, body, timeout):
        if body is not None and hasattr(body, "read"):
            body = body.read()
        self.calls.append({
            "target": target, "method": method, "path": path,
            "headers": dict(headers or {}), "body": body,
            "timeout": timeout,
        })

    def _step(self):
        if not self._script:
            raise AssertionError("Recorder: no queued response left")
        step = self._script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step

    def dial(self, target, method, path, headers=None, body=None, timeout=None):
        self._record(target, method, path, headers, body, timeout)
        return self._step()

    def dial_stream(self, target, method, path, headers=None, body=None,
                    timeout=None):
        self._record(target, method, path, headers, body, timeout)
        step = self._step()
        if isinstance(step, ReplayResponse):
            return step
        status, resp_headers, resp_body = step
        return ReplayResponse(status, resp_headers, resp_body)


@pytest.fixture()
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(transport, "dial", rec.dial)
    monkeypatch.setattr(transport, "dial_stream", rec.dial_stream)
    return rec


JSON = {"Content-Type": "application/json"}


def ok(payload: dict, status: int = 200):
    return (status, JSON, json.dumps(payload).encode())


# -- announce -----------------------------------------------------------------

def test_register_once_routes_through_dial(recorder):
    from livestack_node import announce

    recorder.queue(ok({"registered": True}))
    out = announce.register_once(
        "http://facade:1/x", host_id="h1", kind="asr",
        broker="http://broker:1", timeout=3)
    assert out == {"registered": True}
    (call,) = recorder.calls
    assert call["target"] == "http://broker:1"
    assert call["method"] == "POST"
    assert call["path"] == "/peers"
    assert call["headers"] == JSON
    assert json.loads(call["body"].decode())["host_id"] == "h1"
    assert call["timeout"] == 3


def test_register_once_surfaces_last_error_through_dial(recorder):
    from livestack_node import announce

    recorder.queue(urllib.error.URLError("down"))
    with pytest.raises(urllib.error.URLError):
        announce.register_once("http://f:1", host_id="h", kind="asr",
                               broker="http://b:1")


def test_facade_answers_routes_through_dial(recorder):
    from livestack_node import announce

    recorder.queue(ok({}))
    assert announce.facade_answers("http://facade:1") is True
    (call,) = recorder.calls
    assert (call["method"], call["path"]) == ("GET", "/residence")

    recorder.queue(urllib.error.HTTPError(
        "http://facade:1/residence", 503, "no", {}, io.BytesIO(b"")))
    assert announce.facade_answers("http://facade:1") is False


# -- client -------------------------------------------------------------------

def test_client_admit_routes_through_dial(recorder):
    from livestack_node import client

    recorder.queue(ok({"granted": True, "device_id": "cuda:0"}))
    out = client.admit("asr", brokers=["http://broker:1"], token="tok")
    assert out == {"granted": True, "device_id": "cuda:0"}
    (call,) = recorder.calls
    assert (call["target"], call["method"], call["path"]) == \
        ("http://broker:1", "POST", "/admit")
    assert call["headers"]["Authorization"] == "Bearer tok"


def test_client_rank_snapshot_routes_through_dial(recorder):
    from livestack_node import client

    recorder.queue(ok({"targets": []}))
    out = client.rank_snapshot("tts", brokers=["http://broker:1"],
                               vantage="host:h1", asker_region="na")
    assert out == {"targets": []}
    (call,) = recorder.calls
    assert call["method"] == "GET"
    assert call["path"] == "/fleet/rank?kind=tts&via=host%3Ah1&region=na"


def test_client_lease_lifecycle_routes_through_dial(recorder):
    from livestack_node import client

    recorder.queue(ok({"lease_id": "l1"}), ok({"ok": True}), ok({"ok": True}))
    with client.lease("asr", base_url="http://node:1/livestack",
                      heartbeat_interval=0.05) as res:
        assert res["lease_id"] == "l1"
        deadline = time.monotonic() + 2
        while len(recorder.calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)  # let the heartbeat thread fire once
    paths = [c["path"] for c in recorder.calls]
    assert paths == ["/livestack/lease",
                     "/livestack/lease/l1/heartbeat",
                     "/livestack/lease/l1/release"]
    assert all(c["method"] == "POST" for c in recorder.calls)


# -- hostbroker ---------------------------------------------------------------

def test_hostbroker_http_helper_routes_through_dial(recorder):
    from livestack_node import hostbroker

    recorder.queue(ok({"host_id": "n1"}))
    out = hostbroker._http("http://node:1/peers", timeout=5)
    assert out == {"host_id": "n1"}
    (call,) = recorder.calls
    assert (call["method"], call["path"], call["timeout"]) == ("GET", "/peers", 5)

    recorder.queue(ok({"units": []}))
    hostbroker._http("http://node:1/model/evict", {"unit": "asr"},
                     headers={"Authorization": "Bearer t"})
    call = recorder.calls[-1]
    assert call["method"] == "POST"
    assert call["headers"]["Authorization"] == "Bearer t"


def test_rest_peer_probe_routes_through_dial(recorder):
    from livestack_node import hostbroker

    recorder.queue(ok({"host_id": "n1"}))
    peer = hostbroker.RestPeer("http://node:1/")
    assert peer.refresh() == {"host_id": "n1"}
    (call,) = recorder.calls
    assert (call["method"], call["path"]) == ("GET", "/residence")


def test_measure_links_routes_through_dial(recorder):
    from livestack_node import hostbroker

    recorder.queue(ok({"host_id": "other", "links": {"h1": 12.0}}))
    broker = hostbroker.HostBroker.__new__(hostbroker.HostBroker)
    broker.link_peers = ["http://peer:1"]
    broker.link_ms, broker.peer_links = {}, {}
    broker._last_probe_error = {}
    measured = hostbroker.HostBroker.measure_links(broker)
    assert list(measured) == ["other"]
    assert broker.peer_links["other"] == {"h1": 12.0}
    (call,) = recorder.calls
    assert (call["method"], call["path"]) == ("GET", "/peers")


# -- perception ---------------------------------------------------------------

def test_perception_post_json_routes_through_dial(recorder):
    from livestack_node.perception import serve

    recorder.queue(ok({"granted": True}))
    out = serve._post_json("http://broker:1/admit", {"kind": "x"}, "tok")
    assert out == {"granted": True}
    (call,) = recorder.calls
    assert (call["target"], call["method"], call["path"]) == \
        ("http://broker:1", "POST", "/admit")
    assert call["headers"]["Authorization"] == "Bearer tok"


def test_perception_post_json_surfaces_dial_failure(recorder):
    from livestack_node.perception import serve
    from livestack_node.perception.contract import PerceptionContractError

    recorder.queue(urllib.error.URLError("down"))
    with pytest.raises(PerceptionContractError) as err:
        serve._post_json("http://broker:1/admit", {})
    assert err.value.status == 503 and err.value.retryable


def test_remote_infer_and_cancel_route_through_the_seam(recorder):
    from livestack_node.perception import remote

    gate = {"release": threading.Event(), "cancel": False}

    class Control:
        cause = "caller"

        def cancelled(self):
            return gate["cancel"]

    # The infer stream stays blocked until the test releases it, so the
    # monitor thread observes the cancellation and fires the cancel dial
    # while the outbound request is still in flight.
    resp = ReplayResponse(200, JSON, json.dumps({"result": "ok"}).encode())
    real_read = resp.read

    def gated_read(n=-1):
        if not gate["release"].wait(5):
            raise TimeoutError("test gate never released")
        return real_read(n)

    resp.read = gated_read
    recorder.queue(resp, (200, {}, b"{}"))
    adapter = remote.RemotePerceptionAdapter(url="http://mac:1/v1",
                                             token="t", timeout=30)
    result = {}

    def run():
        result["out"] = adapter.infer({"requestId": "q1"},
                                      grant={"realm": "r", "owner": "o"},
                                      control=Control())

    worker = threading.Thread(target=run)
    worker.start()
    for _ in range(200):
        if recorder.calls:
            break
        time.sleep(0.01)
    assert recorder.calls[0]["path"] == "/v1"
    gate["cancel"] = True
    for _ in range(200):
        if len(recorder.calls) >= 2:
            break
        time.sleep(0.01)
    gate["release"].set()
    worker.join(5)
    assert result["out"] == {"result": "ok"}
    _outbound, cancel = recorder.calls
    assert (cancel["method"], cancel["path"]) == ("POST", "/v1/q1/cancel")


# -- workloads ----------------------------------------------------------------

def test_workload_client_routes_through_dial(recorder):
    from livestack_node.workloads.client import WorkloadClient
    from livestack_node.workloads.model import WorkloadError

    client = WorkloadClient("http://authority:1", "x" * 40)
    recorder.queue(ok({"job_id": "j1"}))
    out = client.request("jobs", {"kind": "asr"})
    assert out == {"job_id": "j1"}
    (call,) = recorder.calls
    assert (call["method"], call["path"]) == ("POST", "/v1/workloads/jobs")
    assert call["headers"]["Authorization"] == "Bearer " + "x" * 40

    err_body = json.dumps({"error": "quota"}).encode()
    recorder.queue(urllib.error.HTTPError(
        "http://authority:1/v1/workloads/jobs", 429, "quota", {},
        io.BytesIO(err_body)))
    with pytest.raises(WorkloadError) as err:
        client.request("jobs", {"kind": "asr"})
    assert err.value.status == 429


def test_lease_helper_routes_through_dial(recorder):
    from livestack_node.workloads import lease_helper

    recorder.queue(ok({"granted": True, "lease_id": "l9"}), ok({"ok": True}))
    with tempfile.TemporaryDirectory() as tmp:
        out = lease_helper.admit("http://fleet:1", "tok", "me", "asr",
                                 output_dir=tmp)
    assert out["lease_id"] == "l9"
    (call,) = recorder.calls
    assert (call["target"], call["method"], call["path"]) == \
        ("http://fleet:1", "POST", "/fleet/admit")

    ok_flag = lease_helper.heartbeat("http://fleet:1", "l9")
    assert ok_flag is True
    assert recorder.calls[-1]["path"] == "/lease/l9/heartbeat"


def test_transfer_put_routes_through_dial(recorder, tmp_path):
    from livestack_node.workloads.client import WorkloadClient
    from livestack_node.workloads.transfer import InputTransfer

    payload = b"input bytes"
    source = tmp_path / "in.bin"
    source.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    client = WorkloadClient("http://authority:1", "x" * 40)
    transfer = InputTransfer(client)
    recorder.queue(ok({"digest": digest, "size": len(payload)}))
    result = transfer.put(source)
    assert result["digest"] == digest
    (call,) = recorder.calls
    assert (call["method"], call["path"]) == ("PUT", f"/v1/workloads/objects/{digest}")
    assert call["body"] == payload  # the file stream rode the seam as the body
    assert call["headers"]["Content-Length"] == str(len(payload))


def test_download_routes_through_dial_stream(recorder):
    import io

    from livestack_node.workloads.client import WorkloadClient
    from livestack_node.workloads.download import download_into

    payload = b"downloaded object"
    digest = hashlib.sha256(payload).hexdigest()
    client = WorkloadClient("http://authority:1", "x" * 40)
    recorder.queue((200, {
        "Content-Length": str(len(payload)),
        "ETag": '"' + digest + '"',
    }, payload))
    out = io.BytesIO()
    download_into(client, digest, {}, out, max_bytes=1024 * 1024)
    assert out.getvalue() == payload
    (call,) = recorder.calls
    assert (call["method"], call["path"]) == ("GET", f"/v1/workloads/objects/{digest}")


# -- policy_lab ---------------------------------------------------------------

def test_profile_worker_metadata_routes_through_dial(recorder):
    from livestack_node.policy_lab import profile_worker

    recorder.queue(ok({"inflight": 0}))
    out = profile_worker._json("http://engine:1/health")
    assert out == {"inflight": 0}
    (call,) = recorder.calls
    assert (call["method"], call["path"], call["timeout"]) == ("GET", "/health", 10)


def test_profile_worker_post_routes_through_dial_stream(recorder):
    from livestack_node.policy_lab import profile_worker

    chunks = b"x" * (64 * 1024) + b"y" * 100
    recorder.queue((200, JSON, chunks))
    stats = profile_worker._post("http://engine:1/tts/stream", b"body",
                                 "application/json")
    (call,) = recorder.calls
    assert (call["method"], call["path"]) == ("POST", "/tts/stream")
    assert call["body"] == b"body"
    assert stats["request_bytes"] == 4
    assert stats["response_bytes"] == len(chunks)
    assert stats["completion_us"] >= stats["first_output_us"] >= 0
