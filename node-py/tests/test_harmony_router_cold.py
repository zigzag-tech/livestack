"""Every target cold: the router waits for ONE load and serves, never a bare 503.

2026-11-21 17:28-17:31 CST, router on lappy-bellinzona: no serving node held
flash_next (idle eviction). The router ranked zz-whitby first, routed it
`expect=cold`, and zz-whitby's admission refused in ~1 s ("the planner could not
place it on any device"). A pre-byte 503 from an LLM target was handed straight
to the caller, so every chat completion 503'd for two minutes while zz-leaky,
ranked second, would have cold-loaded in ~28 s. A cold target's refusal has cost
the caller no byte and no load: the request goes to the next cold target, which
loads once and serves it (and every concurrent request) within the wait budget.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_harmony_router import _Rows, router  # noqa: F401  (the router fixture)
import fleet_route

CHUNKS = [b'data: {"choices":[{"delta":{"content":"he"}}],"harmony":{"t_ms":1}}\n\n',
          b'data: {"choices":[{"delta":{"content":"llo"}}],"harmony":{"t_ms":2}}\n\n',
          b"data: [DONE]\n\n"]
ANSWER = b'{"id":"x","choices":[{"message":{"content":"hello"}}]}'


class _ColdNode:
    """A node holding nothing. `refuse`: its admission refuses (503 before any
    byte). Otherwise the first request starts a `load_s` load that concurrent
    requests share (one load, as the node's `_gpu_call` serialises `ensure`)."""

    def __init__(self, refuse=False, load_s=0.0):
        self.got, self.loads, outer = [], 0, self
        self._lock, self._loaded = threading.Lock(), threading.Event()

        def ensure():
            with outer._lock:
                if outer._loaded.is_set():
                    return
                outer.loads += 1
                time.sleep(load_s)
                outer._loaded.set()

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, status, ctype, out):
                self.send_response(status)
                self.send_header("content-type", ctype)
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.got.append({"raw": raw, "routed": self.headers.get("x-harmony-routed")})
                if refuse:
                    return self._send(503, "application/json", json.dumps({
                        "detail": "flash_next was not admitted: the planner could not "
                                  "place it on any device"}).encode())
                ensure()
                if json.loads(raw or b"{}").get("stream"):
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for c in CHUNKS:
                        self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    self._send(200, "application/json", ANSWER)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def row(self):
        return {"peer": f"{self.base}/livestack", "state": "fresh", "ready": False,
                "kinds": ["llm"], "region": "na", "detail": "no unit resident",
                "units": [{"kind": "flash_next", "resident": False,
                           "attributes": {"class": "llm", "tools": True, "params_b": 125}}]}

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def cold_fleet(router, monkeypatch):  # noqa: F811
    made = []

    def build(*nodes):
        """`nodes` in the order the ranking must try them (ties break on target id)."""
        made.extend(nodes)
        ordered = sorted(n.base for n in nodes)
        for n, base in zip(nodes, ordered):
            assert n.base == base, "test setup: ranking order is target-id order"
        view = {"generated_at": time.time(),
                "hosts": {f"h{i}": {"nodes": [n.row()]} for i, n in enumerate(nodes)}}
        monkeypatch.setattr(router, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(fleet_route, "fleet_view", lambda url, *a, **k: view)
        monkeypatch.setattr(fleet_route, "mark_cold", lambda *a: None)
        monkeypatch.setattr(fleet_route, "mark_busy", lambda *a: None)
        routes = _Rows()
        monkeypatch.setattr(router, "ROUTES", routes)
        monkeypatch.setattr(router, "_PRINCIPALS", None)
        from fastapi.testclient import TestClient
        return TestClient(router.app), routes

    yield build
    for n in made:
        n.close()


def _ranked_pair(**second):
    """(first, second): first refuses its admission, second loads."""
    a, b = _ColdNode(refuse=True), _ColdNode(**second)
    if a.base > b.base:                    # make the refusing node rank first
        a.close(), b.close()
        return _ranked_pair(**second)
    return a, b


BODY = {"model": "require:class=llm,tools=true", "messages": [{"role": "user", "content": "x"}]}


def test_a_lone_cold_target_is_waited_for_and_serves(cold_fleet):
    node = _ColdNode(load_s=0.5)
    client, routes = cold_fleet(node)
    t0 = time.monotonic()
    r = client.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 200 and r.content == ANSWER
    assert time.monotonic() - t0 >= 0.5 and node.loads == 1
    assert node.got[0]["routed"].endswith("expect=cold;v=1")


def test_a_cold_target_that_refuses_admission_fails_over_to_the_next_cold_one(cold_fleet):
    refusing, loading = _ranked_pair(load_s=0.5)
    client, routes = cold_fleet(refusing, loading)
    r = client.post("/v1/chat/completions", json=BODY)
    assert r.status_code == 200 and r.content == ANSWER          # not the 503 of 2026-11-21
    assert loading.loads == 1
    attempts = routes.rows[-1]["fleet_route"]["attempts"]
    assert [a["target"] for a in attempts] == [refusing.base, loading.base]
    assert attempts[0]["result"].startswith("unavailable") and attempts[1]["result"] == "ok"
    assert loading.got[0]["raw"] == refusing.got[0]["raw"]       # the body as the caller sent it


def test_concurrent_cold_requests_trigger_one_load_and_are_all_served(cold_fleet):
    refusing, loading = _ranked_pair(load_s=0.8)
    client, _ = cold_fleet(refusing, loading)
    results = [None] * 4

    def call(i):
        body = {**BODY, "stream": True} if i % 2 else BODY
        with client.stream("POST", "/v1/chat/completions", json=body) as r:
            results[i] = (r.status_code, b"".join(r.iter_raw()), bool(i % 2))

    threads = [threading.Thread(target=call, args=(i,)) for i in range(len(results))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    for status, raw, streamed in results:
        assert status == 200
        # chunks (and their `harmony.t_ms` stamps) are relayed byte for byte
        assert raw == (b"".join(CHUNKS) if streamed else ANSWER)
    assert loading.loads == 1                   # one load, shared by every request
    assert len(refusing.got) == len(loading.got) == len(results)


def test_a_warm_routed_503_is_still_the_targets_answer(router, monkeypatch):  # noqa: F811
    """The failover is for COLD routes only: a warm target's 503 is not an
    admission refusal and is not turned into a cold load elsewhere."""
    import asyncio
    a, b = _ColdNode(refuse=True), _ColdNode()
    try:
        async def go():
            attempts = []
            resp, client = await fleet_route.send_routed(
                first=(a.base, "warm"), rechoose=lambda tried: (b.base, "cold"),
                router_id="r1", request_id="rid", method="POST", path="chat/completions",
                body=json.dumps(BODY).encode(), headers={}, params={}, timeout=5.0,
                attempts=attempts)
            status = resp.status_code
            await resp.aclose(); await client.aclose()
            return status, attempts
        status, attempts = asyncio.run(go())
        assert status == 503 and len(attempts) == 1 and b.got == []
    finally:
        a.close(), b.close()
