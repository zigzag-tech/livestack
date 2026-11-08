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


def test_facade_specific_endpoints_are_refused_and_named(wired):
    client, node, _ = wired
    r = client.post("/v1/models-admin", json={"x": 1})
    assert r.status_code == 404 and "/v1/models-admin" in r.text and node.got == []


def test_the_classifier_routes_to_a_node_that_has_its_adapter(router, monkeypatch):
    """`/v1/classifier` names its ADAPTER in `model` (`jemm`): the ranking must
    pick a node whose units declare `adapter.jemm`, not whichever ranks first."""
    from fastapi.testclient import TestClient
    has, lacks = _Node(), _Node()
    try:
        a = has.row(); a["units"][0]["attributes"]["adapter.jemm"] = True
        view = {"generated_at": time.time(), "hosts": {"a": {"nodes": [lacks.row()]},
                                                       "b": {"nodes": [a]}}}
        monkeypatch.setattr(router, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(fleet_route, "fleet_view", lambda url, *a, **k: view)
        monkeypatch.setattr(router, "ROUTES", _Rows())
        r = TestClient(router.app).post("/v1/classifier", json={"model": "jemm", "questions": {}})
        assert r.status_code == 200 and len(has.got) == 1 and lacks.got == []
    finally:
        has.close(), lacks.close()


def test_embeddings_follow_the_facades_one_space_rule(router, monkeypatch):
    # Two embedders of DIFFERENT models meet the need: a vector space is a data
    # contract, so the router says so (400) instead of choosing by distance.
    from fastapi.testclient import TestClient

    def emb(base, model):
        return {"peer": f"{base}/livestack", "state": "fresh", "ready": True, "kinds": ["embed"],
                "region": "na", "units": [{"kind": "embed", "resident": True,
                                           "attributes": {"class": "embed", "model": model, "dim": 512}}]}

    sink = _Node()
    try:
        monkeypatch.setattr(router, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(router, "ROUTES", _Rows())
        one = {"generated_at": time.time(), "hosts": {"e": {"nodes": [emb(sink.base, "gemma-2")]}}}
        monkeypatch.setattr(fleet_route, "fleet_view", lambda url, *a, **k: one)
        r = TestClient(router.app).post("/v1/embeddings", json={
            "model": "require:class=embed,dim=512", "input": "hi"})
        assert r.status_code == 200
        assert json.loads(sink.got[0]["raw"])["model"] == "gemma-2"      # the node's own model id
        two = {"generated_at": time.time(), "hosts": {"e": {"nodes": [emb(sink.base, "gemma-2")]},
                                                      "f": {"nodes": [emb("http://127.0.0.1:9", "minilm")]}}}
        monkeypatch.setattr(fleet_route, "fleet_view", lambda url, *a, **k: two)
        r = TestClient(router.app).post("/v1/embeddings", json={
            "model": "require:class=embed,dim=512", "input": "hi"})
        assert r.status_code == 400 and "not comparable" in r.text
    finally:
        sink.close()


def test_two_accounts_sharing_a_router_never_see_each_other(wired, monkeypatch):
    """Per-account isolation: each request's credential and owner reach the target
    as that caller sent them, the route record is attributed to that caller, and
    nothing of one account's request appears on another's."""
    from livestack_node.fleet_auth import Principal
    client, node, routes = wired
    tok_a, tok_b = "tok-account-a-0123456789", "tok-account-b-0123456789"
    import sys
    router = sys.modules["harmony_router_under_test"]
    monkeypatch.setattr(router, "_PRINCIPALS", {tok_a: Principal(name="acct-a", owner="a"),
                                                 tok_b: Principal(name="acct-b", owner="b")})
    client.post("/v1/chat/completions", json=_body(),
                headers={"authorization": f"Bearer {tok_a}", "x-harmony-owner": "tenant-a:acct_1"})
    client.post("/v1/chat/completions", json=_body(),
                headers={"authorization": f"Bearer {tok_b}", "x-harmony-owner": "tenant-b:acct_1"})
    a, b = node.got
    assert a["h"]["authorization"].endswith(tok_a) and a["h"]["x-harmony-owner"] == "tenant-a:acct_1"
    assert b["h"]["authorization"].endswith(tok_b) and b["h"]["x-harmony-owner"] == "tenant-b:acct_1"
    ra, rb = routes.rows
    assert (ra["principal"], rb["principal"]) == ("acct-a", "acct-b")
    assert ra["owner_ns"] and rb["owner_ns"] and ra["owner_ns"] != rb["owner_ns"]
    assert ra["request_id"] != rb["request_id"]
    dump = json.dumps(routes.rows)
    assert tok_a not in dump and tok_b not in dump           # a credential is never logged


# --- speech: one declarative request, the router places and fails over -------
# (openspec change harmony-speech-routing). A speech node's units declare no
# `class` attribute, so these also pin that the llm default clause is not applied.

class _SpeechNode:
    """A polyasr node: answers batch transcription, or 503 device_busy."""

    def __init__(self, busy=False, retry_after="30", bare_503=False):
        self.got, outer = [], self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("content-length") or 0)
                outer.got.append({"raw": self.rfile.read(n), "path": self.path,
                                  "h": {k.lower(): v for k, v in self.headers.items()}})
                if busy or bare_503:
                    out = (b'{"status":"device_busy","detail":"device held exclusively by harmony-image"}'
                           if busy else b'{"detail":"boom"}')
                    self.send_response(503)
                    if busy:
                        self.send_header("retry-after", retry_after)
                else:
                    out = b'{"text":"hello"}'
                    self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def row(self, region="na", in_flight=0):
        return {"peer": f"{self.base}/livestack", "state": "fresh", "ready": True,
                "kinds": ["polyasr"], "region": region, "detail": "Harmony process-managed provider",
                "load": {"in_flight": in_flight},
                "units": [{"kind": "asr", "resident": True}, {"kind": "align", "resident": False}]}

    def close(self):
        self.srv.shutdown()


@pytest.fixture
def speech(router, monkeypatch):
    nodes = []

    def build(*specs):
        # specs: (SpeechNode kwargs, row kwargs); earlier = lower load = ranked first
        view = {"generated_at": time.time(), "hosts": {}}
        for i, (nk, rk) in enumerate(specs):
            n = _SpeechNode(**nk)
            nodes.append(n)
            view["hosts"][f"h{i}"] = {"nodes": [n.row(**rk)]}
        monkeypatch.setattr(router, "FLEET_URL", "http://fleet.invalid:8801")
        monkeypatch.setattr(fleet_route, "fleet_view", lambda url, *a, **k: view)
        monkeypatch.setattr(fleet_route, "mark_busy", lambda *a: None)
        routes = _Rows()
        monkeypatch.setattr(router, "ROUTES", routes)
        monkeypatch.setattr(router, "_PRINCIPALS", None)
        from fastapi.testclient import TestClient
        return TestClient(router.app), nodes, routes

    yield build
    for n in nodes:
        n.close()


_WAV = {"file": ("a.wav", b"RIFFxxxxWAVE", "audio/wav")}


def test_a_busy_speech_node_is_failed_over_to_the_next_provider(speech):
    client, (busy, ok), routes = speech(({"busy": True}, {"in_flight": 0}), ({}, {"in_flight": 1}))
    r = client.post("/v1/audio/transcriptions", files=_WAV)
    assert r.status_code == 200 and r.json() == {"text": "hello"}
    assert len(busy.got) == 1 and len(ok.got) == 1
    # the audio the second node got is the audio the caller sent
    assert ok.got[0]["raw"] == busy.got[0]["raw"] and ok.got[0]["path"] == "/v1/audio/transcriptions"
    attempts = routes.rows[-1]["fleet_route"]["attempts"]
    assert [a["result"].split(":")[0] for a in attempts] == ["busy", "ok"]


def test_a_plain_503_is_also_failed_over_but_not_called_busy(speech):
    client, (dead, ok), routes = speech(({"bare_503": True}, {"in_flight": 0}), ({}, {"in_flight": 1}))
    assert client.post("/v1/audio/transcriptions", files=_WAV).status_code == 200
    assert routes.rows[-1]["fleet_route"]["attempts"][0]["result"].startswith("unavailable")


def test_when_no_speech_provider_can_serve_the_client_gets_only_unavailable_and_retry_after(speech, capfd):
    client, nodes, routes = speech(({"busy": True, "retry_after": "45"}, {"in_flight": 0}),
                                   ({"busy": True, "retry_after": "20"}, {"in_flight": 1}))
    r = client.post("/v1/audio/transcriptions", files=_WAV, headers={"x-request-id": "req-abc.1"})
    assert r.status_code == 503 and r.headers["retry-after"] == "20"
    assert r.json() == {"error": "unavailable", "request_id": "req-abc.1"}
    # the diagnosis moved, it did not vanish: server-side log by request id + decision record
    logged = [json.loads(l) for l in capfd.readouterr().out.splitlines() if "router_public_error" in l]
    assert logged and logged[-1]["request_id"] == "req-abc.1"
    assert "route_failed" in logged[-1]["detail"] and "harmony-image" in logged[-1]["detail"]
    attempts = routes.rows[-1]["fleet_route"]["attempts"]
    assert len(attempts) == 2 and "harmony-image" in attempts[0]["result"]
    assert routes.rows[-1]["outcome"] == "refused"


def test_a_retry_after_less_failure_still_tells_the_client_when_to_retry(speech):
    client, _, _ = speech()
    r = client.post("/v1/audio/transcriptions", files=_WAV)
    assert r.status_code == 503 and r.headers["retry-after"] == "30"
    assert r.json()["error"] == "unavailable" and r.json()["request_id"]


def test_the_attempt_budget_is_bounded(speech):
    client, nodes, _ = speech(*[({"busy": True}, {"in_flight": i}) for i in range(5)])
    r = client.post("/v1/audio/transcriptions", files=_WAV)
    assert r.status_code == 503 and sum(len(n.got) for n in nodes) == 3   # HARMONY_ROUTER_SPEECH_ATTEMPTS


def test_a_speech_request_with_no_node_says_so(speech):
    client, _, _ = speech()
    r = client.post("/v1/audio/transcriptions", files=_WAV)
    assert r.status_code == 503 and r.json()["error"] == "unavailable"
    assert "no polyasr target" not in r.text


def test_llm_503_is_not_retried_by_the_speech_rule(wired):
    # the speech retry is opt-in: an llm 503 still reaches the caller untouched
    client, node, _ = wired
    r = client.post("/v1/chat/completions", json={"model": "require:class=llm,params_b>=20",
                                                  "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200 and len(node.got) == 1


def test_batch_asr_logs_never_hold_the_audio_or_the_form_fields(speech, router, monkeypatch, tmp_path):
    """Privacy: the placement and route logs record features and outcomes only."""
    from livestack_node import placement
    from livestack_node.demand_log import demand_log_from_env
    monkeypatch.setenv("HARMONY_DEMAND_LOG_DIR", str(tmp_path))
    monkeypatch.setenv("HARMONY_DEMAND_LOG_AGE_DAYS", "1")
    log = demand_log_from_env("placement")
    placement.set_decision_log(log)
    routes = demand_log_from_env("routes")
    client, (busy, ok), _ = speech(({"busy": True}, {"in_flight": 0}), ({}, {"in_flight": 1}))
    monkeypatch.setattr(router, "ROUTES", routes)
    try:
        r = client.post("/v1/audio/transcriptions", files={"file": ("a.wav", b"RIFFSECRET-AUDIO", "audio/wav")},
                        data={"context": "SECRET-CONTEXT /home/me/private-pane"})
        assert r.status_code == 200
        log.flush()
        routes.flush()
        text = b"".join(p.read_bytes() for p in tmp_path.glob("*.jsonl*"))
        assert b"decision" in text and b"outcome" in text
        for secret in (b"SECRET-AUDIO", b"SECRET-CONTEXT", b"private-pane"):
            assert secret not in text, secret
        assert b"SECRET-AUDIO" in ok.got[0]["raw"]          # positive control: it did reach the node
    finally:
        placement.set_decision_log(None)


def test_health_is_speech_aware_when_asked(speech, router, monkeypatch):
    # default: unchanged, ok with no fleet opinion
    client, nodes, _ = speech(({"busy": True}, {}))
    assert client.get("/health").status_code == 200
    monkeypatch.setattr(router, "HEALTH_KIND", "polyasr")
    r = client.get("/health")                       # a ready node exists: ok, counted
    assert r.status_code == 200 and r.json()["providers"] == 1
    client2, _, _ = speech()                        # an empty fleet: not ok, and says why
    r = client2.get("/health")
    assert r.status_code == 503 and r.json()["status"] == "no_provider" and "polyasr" in r.json()["detail"]
    monkeypatch.setattr(fleet_route, "fleet_view", lambda *a, **k: None)
    assert client2.get("/health").json()["status"] == "no_fleet_view"


def test_a_background_refresher_keeps_the_view_fresh_so_requests_never_fetch(router):
    srv_view = {"generated_at": 1.0, "hosts": {"h": {"nodes": []}}}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            out = json.dumps(srv_view).encode()
            self.send_response(200)
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}"
    stop = fleet_route.start_refresher(url, every_s=0.05, timeout_s=2)
    try:
        deadline = time.time() + 3
        while fleet_route.view_age_s(url) is None and time.time() < deadline:
            time.sleep(0.02)
        assert fleet_route.view_age_s(url) is not None
        srv.shutdown()                          # broker gone: the last good view still serves
        assert fleet_route.fleet_view(url) is not None
    finally:
        stop.set()


# --- A.6: nothing about the fleet reaches a client ------------------------------
# Strings seeded into every upstream failure; none may appear in any response.
LEAKS = ("ENGINE-ID-7f3a", "gpu-host-secret.internal", "harmony-image", "10.99.88.77",
         "RTX-SECRET-DEVICE")


class _LeakyNode(_SpeechNode):
    """Fails in every way a real engine does, naming engine/host/device in the
    body AND in headers (Server, X-Harmony-*)."""

    def __init__(self, status=503, retry_after=None):
        self.got, outer = [], self
        body = json.dumps({"status": "device_busy", "engine": "ENGINE-ID-7f3a",
                           "host": "gpu-host-secret.internal", "ip": "10.99.88.77",
                           "detail": "device RTX-SECRET-DEVICE held exclusively by harmony-image"}).encode()

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers.get("content-length") or 0))
                outer.got.append(1)
                self.send_response(status)
                self.send_header("server", "ENGINE-ID-7f3a/1.0")
                self.send_header("x-harmony-engine", "ENGINE-ID-7f3a")
                self.send_header("x-harmony-host", "gpu-host-secret.internal")
                if retry_after:
                    self.send_header("retry-after", retry_after)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"


def _whole_response(r) -> str:
    return r.text + "\n" + "\n".join(f"{k}: {v}" for k, v in r.headers.items())


def _leaky_world(router, monkeypatch, *nodes):
    view = {"generated_at": time.time(), "hosts": {
        f"gpu-host-secret.internal-{i}": {"nodes": [n.row(in_flight=i)]} for i, n in enumerate(nodes)}}
    monkeypatch.setattr(router, "FLEET_URL", "http://fleet.invalid:8801")
    monkeypatch.setattr(fleet_route, "fleet_view", lambda url, *a, **k: view)
    monkeypatch.setattr(fleet_route, "mark_busy", lambda *a: None)
    monkeypatch.setattr(router, "ROUTES", _Rows())
    monkeypatch.setattr(router, "_PRINCIPALS", None)
    from fastapi.testclient import TestClient
    return TestClient(router.app)


@pytest.mark.parametrize("status,retry_after,expect", [
    (503, "20", "unavailable"), (503, None, "unavailable"), (500, None, "unavailable"),
    (401, None, "unauthorized"), (429, None, "rate_limited"), (413, None, "too_large"),
    (400, None, "bad_request"), (422, None, "bad_request")])
def test_no_upstream_failure_text_or_header_reaches_the_client(router, monkeypatch, status, retry_after, expect):
    node = _LeakyNode(status, retry_after)
    try:
        client = _leaky_world(router, monkeypatch, node)
        r = client.post("/v1/audio/transcriptions", files=_WAV)
        assert r.json()["error"] == expect and r.status_code == router.PUBLIC_VOCAB[expect]
        whole = _whole_response(r)
        assert node.got                                        # the failure really happened
        for leak in LEAKS:
            assert leak not in whole, leak
        assert "server" not in r.headers or "ENGINE" not in r.headers["server"]
        assert not [h for h in r.headers if h.lower().startswith("x-harmony")]
        assert (expect == "unavailable") == ("retry-after" in r.headers)
    finally:
        node.close()


def test_exhausted_failover_over_leaky_nodes_leaks_nothing(router, monkeypatch):
    nodes = [_LeakyNode(503, "20"), _LeakyNode(503, "45"), _LeakyNode(503)]
    try:
        client = _leaky_world(router, monkeypatch, *nodes)
        r = client.post("/v1/audio/transcriptions", files=_WAV)
        assert r.status_code == 503 and len(sum(([n.got for n in nodes]), [])) >= 2   # failover kept working
        assert r.json()["error"] == "unavailable" and r.headers["retry-after"] == "20"
        for leak in LEAKS:
            assert leak not in _whole_response(r), leak
    finally:
        for n in nodes:
            n.close()


def test_a_successful_answer_carries_no_upstream_server_or_harmony_headers(speech, monkeypatch):
    client, (ok,), _ = speech(({}, {}))
    r = client.post("/v1/audio/transcriptions", files=_WAV)
    assert r.status_code == 200 and r.json() == {"text": "hello"}
    assert not [h for h in r.headers if h.lower() in ("server", "via") or h.lower().startswith("x-harmony")]


def test_positive_control_the_leak_test_fails_when_the_scrub_is_removed(router, monkeypatch):
    # Without the scrub the same fixture DOES leak, so the assertions above can fail.
    node = _LeakyNode(503, "20")
    try:
        client = _leaky_world(router, monkeypatch, node)
        monkeypatch.setattr(router, "_public_response",
                            lambda rid, status, detail, headers=None, surface="http":
                            router.JSONResponse({"detail": detail}, status_code=status))
        r = client.post("/v1/audio/transcriptions", files=_WAV)
        assert any(leak in _whole_response(r) for leak in LEAKS)
    finally:
        node.close()


def test_request_ids_are_tokens_never_free_text(router):
    assert router._request_id("abc-1.2_X") == "abc-1.2_X"
    for bad in ("", None, "a b", "x" * 65, "<script>", "host: gpu-host-secret.internal"):
        out = router._request_id(bad)
        assert out != bad and router._REQUEST_ID_OK.match(out)


def test_public_all_covers_health_explain_and_unknown_paths(router, monkeypatch):
    monkeypatch.setattr(router, "PUBLIC_ALL", True)
    monkeypatch.setattr(router, "HEALTH_KIND", "polyasr")
    client = _leaky_world(router, monkeypatch)             # no nodes: no provider
    h = client.get("/health")
    assert h.status_code == 503 and h.json() == {"error": "unavailable"}
    assert client.post("/harmony/route", json={}).json()["error"] == "bad_request"
    r = client.get("/v1/not-a-thing")
    assert r.json()["error"] == "bad_request" and "harmony-router routes" not in r.text


def test_public_off_keeps_the_llm_router_detail_for_operators(wired):
    client, _, _ = wired
    r = client.get("/v1/not-a-thing")
    assert r.status_code == 404 and "harmony-router routes" in r.text


def test_a_public_health_names_neither_the_fleet_nor_its_pool(router, monkeypatch):
    # A probe behind a public edge reads up/down. router id, region, fleet URL, contract,
    # pool size and the no_fleet_view/no_provider words all say something about the fleet.
    monkeypatch.setattr(router, "PUBLIC_ALL", True)
    monkeypatch.setattr(router, "HEALTH_KIND", "polyasr")
    monkeypatch.setattr(router, "ROUTER_ID", "router-secret-host-7")
    monkeypatch.setattr(router, "REGION", "secret-region")
    node = _LeakyNode()
    client = _leaky_world(router, monkeypatch, node)
    ok = client.get("/health")
    assert ok.status_code == 200 and set(ok.json()) == {"status", "revision"}
    for needle in ("secret", "fleet.invalid", "providers", "polyasr"):
        assert needle not in ok.text
    monkeypatch.setattr(fleet_route, "fleet_view", lambda *a, **k: None)
    down = client.get("/health")
    assert down.status_code == 503 and down.json() == {"error": "unavailable"}
    assert "Retry-After" in down.headers or "retry-after" in down.headers


def test_an_unhandled_bug_on_a_public_path_answers_from_the_vocabulary(router, monkeypatch, capfd):
    monkeypatch.setattr(router, "PUBLIC_ALL", True)
    client = _leaky_world(router, monkeypatch)

    async def boom(*a, **k):
        raise RuntimeError("gpu-host-secret.internal: engine qwen3-asr-1.7b pool=asr-scarce")
    monkeypatch.setattr(router, "_route", boom)
    from fastapi.testclient import TestClient
    r = TestClient(router.app, raise_server_exceptions=False).post("/v1/audio/transcriptions", content=b"x")
    assert r.status_code == 503 and r.json()["error"] == "unavailable"
    assert "secret" not in r.text and "qwen" not in r.text and "asr-scarce" not in r.text
    assert "server" not in {k.lower() for k in r.headers}
    assert "gpu-host-secret.internal" in capfd.readouterr().out      # cause kept in the router's own log
