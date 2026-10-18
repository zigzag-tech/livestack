"""The localhost router: a facade with zero units (harmony-regional-llm-routing).

It routes every request, is never a routing target, holds no fleet credential,
and forwards bytes untouched."""
import importlib.util
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm"
sys.path.insert(0, str(HERE))
import fleet_route  # noqa: E402


@pytest.fixture(scope="module")
def router():
    os.environ.setdefault("HARMONY_ROUTER_ID", "test-router")
    spec = importlib.util.spec_from_file_location("harmony_router_under_test", HERE / "router.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_router_under_test"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        pytest.skip(f"router not importable here: {e}")
    return module


class _Node:
    """A serving node: streams a fixed chunked body for chat, records requests."""
    CHUNKS = [b'data: {"choices":[{"delta":{"content":"he"}}]}\n\n',
              b'data: {"choices":[{"delta":{"content":"llo"}}]}\n\n', b"data: [DONE]\n\n"]

    def __init__(self, stream_status=200):
        self.got, outer = [], self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n)
                outer.got.append({"raw": raw, "h": {k.lower(): v for k, v in self.headers.items()}})
                if json.loads(raw or b"{}").get("stream"):
                    self.send_response(stream_status)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for c in outer.CHUNKS:
                        self.wfile.write(f"{len(c):x}\r\n".encode() + c + b"\r\n")
                        self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                else:
                    out = b'{"id":"x","choices":[{"message":{"content":"hello"}}]}'
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def row(self, region="na"):
        return {"peer": f"{self.base}/livestack", "state": "fresh", "ready": True,
                "kinds": ["llm"], "region": region, "detail": "resident",
                "units": [{"kind": "u", "resident": True,
                           "attributes": {"class": "llm", "params_b": 27}}]}

    def close(self):
        self.srv.shutdown()


class _Rows:
    def __init__(self):
        self.rows = []

    def record(self, **f):
        self.rows.append(f)


@pytest.fixture
def wired(router, monkeypatch):
    node = _Node()
    view = {"generated_at": time.time(), "hosts": {"gpu": {"nodes": [node.row()]}}}
    monkeypatch.setattr(router, "FLEET_URL", "http://fleet.invalid:8801")
    monkeypatch.setattr(fleet_route, "fleet_view", lambda url, *a, **k: view)
    routes = _Rows()
    monkeypatch.setattr(router, "ROUTES", routes)
    monkeypatch.setattr(router, "_PRINCIPALS", None)
    from fastapi.testclient import TestClient
    yield TestClient(router.app), node, routes
    node.close()


def _body(**extra):
    return {"model": "require:class=llm,params_b>=20",
            "messages": [{"role": "user", "content": "x"}], **extra}


def test_a_router_with_no_units_routes_every_request_byte_for_byte(wired):
    client, node, routes = wired
    r = client.post("/v1/chat/completions", json=_body())
    assert r.status_code == 200 and r.content == b'{"id":"x","choices":[{"message":{"content":"hello"}}]}'
    assert json.loads(node.got[0]["raw"])["model"] == "require:class=llm,params_b>=20"   # as asked
    row = routes.rows[-1]
    assert row["role"] == "router" and row["outcome"] == "ok"
    assert row["fleet_route"]["trigger"] == "router" and row["fleet_route"]["expect"] == "warm"
    assert row["request_id"] == node.got[0]["h"]["x-request-id"]


def test_a_stream_is_relayed_chunk_for_chunk(wired):
    client, node, _ = wired
    with client.stream("POST", "/v1/chat/completions", json=_body(stream=True)) as r:
        assert r.status_code == 200
        got = b"".join(r.iter_raw())
    assert got == b"".join(_Node.CHUNKS)


def test_the_target_sees_the_contract_header_and_never_a_router_credential(wired, monkeypatch):
    client, node, _ = wired
    monkeypatch.setenv("HARMONY_LLM_FLEET_TOKEN", "fleet-secret-token-do-not-send")
    client.post("/v1/chat/completions", json=_body(), headers={"authorization": "Bearer caller-token-1234567"})
    client.post("/v1/chat/completions", json=_body())
    with_creds, without = node.got
    assert with_creds["h"]["authorization"] == "Bearer caller-token-1234567"   # the caller's, unchanged
    assert "authorization" not in without["h"]                                 # none invented
    assert "fleet-secret-token" not in json.dumps([with_creds["h"], without["h"]])
    assert with_creds["h"]["x-harmony-routed"] == "test-router;expect=warm;v=1"


def test_a_caller_cannot_forge_the_routed_header(wired):
    client, node, _ = wired
    client.post("/v1/chat/completions", json=_body(), headers={"x-harmony-routed": "me;expect=cold;v=1"})
    assert node.got[0]["h"]["x-harmony-routed"].startswith("test-router;")


def test_health_says_what_a_rollout_checks_and_holds_no_units(router):
    from fastapi.testclient import TestClient
    h = TestClient(router.app).get("/health").json()
    assert h["role"] == "router" and h["units"] == {} and h["contract"] == 1
    assert {"revision", "region", "fleet_url", "router_id"} <= set(h)


def test_the_router_makes_no_registration_call(router, wired, monkeypatch):
    client, node, _ = wired
    seen = []
    real = fleet_route.httpx.AsyncClient.send

    async def spy(self, request, **k):
        seen.append((request.method, str(request.url)))
        return await real(self, request, **k)

    monkeypatch.setattr(fleet_route.httpx.AsyncClient, "send", spy)
    client.post("/v1/chat/completions", json=_body())
    assert all(url.startswith(node.base) for _m, url in seen)      # only ever the target
    assert not hasattr(router, "attach") and not hasattr(router, "announce")


def test_the_router_is_never_its_own_target(wired):
    client, node, _ = wired
    r = client.post("/harmony/route", json=_body())
    fleet = r.json()["fleet"]
    assert fleet["chosen"] == node.base and fleet["decision"] == "forward"
    assert fleet["self"].startswith("router://")


def test_a_dry_run_names_vantage_regions_and_sends_nothing(wired):
    client, node, _ = wired
    fleet = client.post("/harmony/route", json=_body(harmony_regions=["na"])).json()["fleet"]
    assert fleet["regions"] == ["na"] and fleet["regions_source"] == "request" and "vantage" in fleet
    assert node.got == []


def test_no_target_is_a_503_that_says_why_and_leaves_a_route_record(wired):
    client, node, routes = wired
    r = client.post("/v1/chat/completions", json=_body(harmony_regions=["cn"]))
    assert r.status_code == 503 and "cn" in r.text
    assert node.got == [] and routes.rows[-1]["outcome"] == "refused"


def test_without_a_usable_view_the_router_refuses_rather_than_guessing(router, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(router, "FLEET_URL", "http://fleet.invalid:8801")
    monkeypatch.setattr(fleet_route, "fleet_view", lambda url, *a, **k: None)
    monkeypatch.setattr(router, "ROUTES", _Rows())
    r = TestClient(router.app).post("/v1/chat/completions", json=_body())
    assert r.status_code == 503 and "no fleet view" in r.text


def test_the_request_language_is_the_facades(router):
    with pytest.raises(Exception) as e:
        router._plan("chat/completions", {"model": "require:params_b~20"})
    assert "cannot parse requirement clause" in str(e.value.detail)
    req, _prefer, kind = router._plan("chat/completions", {
        "model": "require:class=llm,params_b=[20,30)",
        "messages": [{"content": [{"type": "image_url"}]}]})
    assert req == {"class": "llm", "params_b>=": 20, "params_b<": 30, "vision": True} and kind == "llm"
    assert router._plan("embeddings", {"model": "x"})[2] == "embed"
