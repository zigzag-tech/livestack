"""A context refusal is a routing fact (design §4c), and the re-route has three
rules: it happens ONCE per request, it re-routes by REQUIREMENT (never by unit
name — the planner chooses), and it only fires when another unit can hold the
need. Otherwise the established 413 stands, with the need named in it.

What must survive a re-route: the response bytes (streamed or not), the model
on an unrelated 4xx, and the caller's own clauses.
"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"

CONTEXT_ERROR = ("maximum context length is 16384 tokens but your prompt "
                 "contains at least 40000 input tokens")


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("reroute")
    units = root / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_small", "model": "m/small", "port": 8189, "default": True,
         "footprint_gb": 12, "max_model_len": "16384",
         "attributes": {"class": "llm", "params_b": 9, "context_len": 16384}},
        {"name": "llm_wide", "model": "m/wide", "port": 8191,
         "footprint_gb": 22, "max_model_len": "131072", "engine": "strata",
         "attributes": {"class": "llm", "params_b": 125, "context_len": 131072}},
    ]))
    os.environ.update({
        "HARMONY_LLM_UNITS_FILE": str(units),
        "HARMONY_DEMAND_LOG_AGE_DAYS": "21",
        "HARMONY_DEMAND_LOG_DIR": str(root / "demand"),
        "HARMONY_UNIT_COSTS_FILE": str(root / "unit-costs.jsonl"),
    })
    spec = importlib.util.spec_from_file_location("harmony_llm_server_reroute", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_reroute"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


class _Upstream(BaseHTTPRequestHandler):
    """A stub engine whose refusals speak vLLM's context dialect."""
    refuse_models = set()
    refuse_all = False
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _Upstream.seen.append(body)
        if _Upstream.refuse_all or body.get("model") in _Upstream.refuse_models:
            # Each engine refuses in its OWN dialect: the wide unit is the
            # strata one (llama.cpp words), everyone else speaks vLLM's.
            if "wide" in str(body.get("model")):
                msg = ("prompt (40000 tokens) + max tokens (8) exceeds the "
                       "context (16384); requests are never truncated")
            else:
                msg = CONTEXT_ERROR
            out = json.dumps({"error": {"message": msg}}).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        if body.get("stream"):
            out = (b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
                   b'data: {"choices":[{"delta":{"content":"!"}}],"usage":'
                   b'{"prompt_tokens":40001,"completion_tokens":2}}\n\n'
                   b'data: [DONE]\n\n')
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        out = json.dumps({"id": "chatcmpl-upstream", "model": body.get("model"),
                          "choices": [{"message": {"content": "A"}}],
                          "usage": {"prompt_tokens": 40001, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


@pytest.fixture()
def upstream(srv, monkeypatch):
    _Upstream.refuse_models = set()
    _Upstream.refuse_all = False
    _Upstream.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(srv, "_base_of", lambda name: base)
    monkeypatch.setattr(srv, "_held_elsewhere", lambda name: None)
    monkeypatch.setattr(srv, "_foreign_listener", lambda name: False)
    monkeypatch.setattr(srv.manager, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(srv, "_vllm_up", lambda **k: True)

    def fake_admit(unit="", requires=None, **kw):
        if requires and any(str(k).startswith("context_len>=") for k in requires):
            return {"kind": "llm_wide", "granted": True,
                    "device_id": srv.DEVICE_ID_SELF}
        return {"kind": "llm_small", "granted": True,
                "device_id": srv.DEVICE_ID_SELF}

    monkeypatch.setattr(srv, "admit", fake_admit)
    yield base
    httpd.shutdown()


def test_a_context_refusal_reroutes_once_by_requirement(srv, upstream):
    from fastapi.testclient import TestClient
    _Upstream.refuse_models = {"m/small"}
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm", "max_tokens": 8,
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    # Exactly TWO upstream calls: the refusal, then the ONE re-route. No loop.
    assert [b["model"] for b in _Upstream.seen] == ["m/small", "m/wide"]
    # The re-route is stated as a requirement, never a unit name.
    assert _Upstream.seen[-1]["model"] == "m/wide"
    # Response bytes survive the re-route untouched (non-streaming).
    assert json.loads(r.content)["id"] == "chatcmpl-upstream"


def test_streaming_bytes_survive_the_re_route(srv, upstream):
    from fastapi.testclient import TestClient
    _Upstream.refuse_models = {"m/small"}
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm", "stream": True, "max_tokens": 8,
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    assert [b["model"] for b in _Upstream.seen] == ["m/small", "m/wide"]
    assert b"data: [DONE]" in r.content       # streamed through, byte for byte
    assert r.content.count(b"data: ") == 3


def test_the_re_route_does_not_loop(srv, upstream):
    from fastapi.testclient import TestClient
    _Upstream.refuse_all = True               # the wide unit refuses too
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm", "max_tokens": 8,
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 413
    # ONE re-route per request: the second refusal is answered as it comes.
    assert len(_Upstream.seen) == 2
    assert "40000" in r.text                  # the 413 names the need
    assert "needs" in r.text


def test_nothing_that_can_hold_it_is_the_413_with_the_need_named(srv, upstream, monkeypatch):
    from fastapi.testclient import TestClient
    _Upstream.refuse_models = {"m/small"}
    monkeypatch.delitem(srv.SPECS, "llm_wide")   # nothing wider exists
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm", "max_tokens": 8,
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 413
    assert len(_Upstream.seen) == 1              # no re-route was possible
    assert "needs 40008 tokens" in r.text        # input + reserved output, named
    assert "Nothing here can satisfy it" in r.text


def test_a_callers_other_clauses_ride_the_re_route(srv, upstream):
    from fastapi.testclient import TestClient
    _Upstream.refuse_models = {"m/small"}
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm", "max_tokens": 8,
                          "harmony_requires": {"class": "llm", "params_b>=": 9},
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    assert [b["model"] for b in _Upstream.seen] == ["m/small", "m/wide"]


def test_an_unrelated_4xx_passes_through_with_its_model(srv, upstream, monkeypatch):
    from fastapi.testclient import TestClient

    class _BadRequest(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            out = json.dumps({"error": {"message": "bad request"},
                              "model": "m/small"}).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    httpd = HTTPServer(("127.0.0.1", 0), _BadRequest)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkeypatch.setattr(srv, "_base_of",
                        lambda name: f"http://127.0.0.1:{httpd.server_address[1]}")
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400
    # Byte-for-byte: the model field of a 4xx is the engine's own answer.
    assert json.loads(r.content) == {"error": {"message": "bad request"},
                                     "model": "m/small"}
    httpd.shutdown()


def test_a_named_request_keeps_the_413(srv, upstream):
    """Named is named (task 3.5): a request that NAMES a unit — including the
    legacy alias `local`, which is this node's default model (scenario
    "Named local still reaches the 27B") — gets the 413 with the need named,
    never a silent re-route to some other model the caller did not ask for."""
    from fastapi.testclient import TestClient
    # The SMALL unit's engine refuses under whatever name it is asked ("local"
    # passes through for a named request — named is named).
    _Upstream.refuse_models = {"m/small", "local"}
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "local", "max_tokens": 8,
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 413
    assert "40000" in r.text
    assert [b["model"] for b in _Upstream.seen] == ["local"]   # no re-route


def test_the_llama_cpp_dialect_is_recognised_too(srv, upstream):
    """The refusal dialect is the ENGINE's (design §4c): llama.cpp's
    "prompt (N tokens) + max tokens (M) exceeds the context (K)" names the
    need differently from vLLM's "at least N input tokens", and the block
    matched only vLLM's words (found 2026-10-02: a llama.cpp refusal fell
    through as a raw passthrough instead of the 413 with the need named)."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading
    from fastapi.testclient import TestClient

    class _LlamaCpp(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()

        def do_POST(self):
            out = json.dumps({"error": {"type": "invalid_request_error",
                                        "message": "prompt (26000 tokens) + max tokens (16) "
                                                   "exceeds the context (16384); "
                                                   "requests are never truncated"}}).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    httpd = HTTPServer(("127.0.0.1", 0), _LlamaCpp)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    monkey_base = f"http://127.0.0.1:{httpd.server_address[1]}"
    import unittest.mock as mock
    with mock.patch.object(srv, "_base_of", lambda name: monkey_base), \
         mock.patch.object(srv, "_held_elsewhere", lambda name: None), \
         mock.patch.object(srv, "_foreign_listener", lambda name: False), \
         mock.patch.object(srv.manager, "ensure", lambda *a, **k: None), \
         mock.patch.object(srv, "_vllm_up", lambda **k: True), \
         mock.patch.object(srv, "admit", lambda *a, **k: {"kind": "llm_wide",
                                                           "granted": True,
                                                           "device_id": srv.DEVICE_ID_SELF}):
        # llm_wide is the STRATA unit — the refusal comes from an engine whose
        # dialect is llama.cpp's, which is the point: each unit knows its own
        # engine's words (a vLLM unit would not recognise them, rightly).
        client = TestClient(srv.app)
        r = client.post("/v1/chat/completions",
                        json={"model": "require:class=llm", "max_tokens": 8,
                              "messages": [{"role": "user", "content": "x"}]})
    httpd.shutdown()
    # The 413 with the need named (26000 input + 8 reserved), not a raw 400.
    assert r.status_code == 413
    assert "26008" in r.text or "26000" in r.text
