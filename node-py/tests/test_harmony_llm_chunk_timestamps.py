"""Every streamed chat-completion chunk says when the serving node generated it.

A lossy link between a node and its caller turns a smooth token stream into
bursts. With `"harmony": {"t_ms": N}` on each `data:` chunk (milliseconds since
the node received the request, stamped when the engine's bytes arrived) a client
can re-pace the stream like an RTP playout buffer. These tests drive a fake
engine that streams with known delays through the real proxy.
"""
import importlib.util
import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"

# (delay before writing, bytes). The third chunk's line is split across two
# writes; the stamp is taken when the read that completes it arrives.
SCRIPT = [
    (0.0, b'data: {"choices":[{"delta":{"content":"a"}}]}\n\n'),
    (0.3, b'data: {"choices":[{"delta":{"content":"b"}},], "x":1}\n\n'.replace(b"},]", b"}]")),
    (0.3, b'data: {"choices":[{"delta":{"con'),
    (0.3, b'tent":"c"}}],"harmony":{"node":"n1"}}\n\n: keepalive\n\n'),
    (0.0, b"data: not json\n\n"),
    (0.0, b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":3}}\n\n'),
    (0.0, b"data: [DONE]\n\n"),
]
NON_STREAM = json.dumps({"choices": [{"message": {"content": "A"}}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("stamps")
    units = root / "units.json"
    units.write_text(json.dumps([{
        "name": "llm_general", "model": "m/27b", "port": 8189, "footprint_gb": 21,
        "max_model_len": "24576",
        "attributes": {"class": "llm", "family": "qwen", "params_b": 27}}]))
    os.environ.update({
        "HARMONY_LLM_UNITS_FILE": str(units),
        "HARMONY_DEMAND_LOG_DIR": str(root / "demand"),
        "HARMONY_UNIT_COSTS_FILE": str(root / "unit-costs.jsonl"),
        "HARMONY_LLM_WARM_ON_START": "0",
    })
    spec = importlib.util.spec_from_file_location("harmony_llm_server_stamps", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_stamps"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:                      # shared_py/vLLM absent in this venv
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


class _Engine(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        self.send_response(200)
        if body.get("stream"):
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for delay, data in SCRIPT:
                time.sleep(delay)
                self.wfile.write(data)
                self.wfile.flush()
        else:
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(NON_STREAM)))
            self.end_headers()
            self.wfile.write(NON_STREAM)


@pytest.fixture()
def engine(srv, monkeypatch):
    httpd = HTTPServer(("127.0.0.1", 0), _Engine)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(srv, "_base_of", lambda name: base)
    monkeypatch.setattr(srv, "_held_elsewhere", lambda name: None)
    monkeypatch.setattr(srv, "_held_on_host", lambda name: None)
    monkeypatch.setattr(srv, "_foreign_listener", lambda name: False)
    monkeypatch.setattr(srv.manager, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(srv, "admit", lambda *a, **k: {"kind": "llm_general", "granted": True,
                                                        "device_id": srv.DEVICE_ID_SELF})
    yield base
    httpd.shutdown()


def _post(srv, stream):
    from fastapi.testclient import TestClient
    return TestClient(srv.app).post("/v1/chat/completions", json={
        "model": "llm_general", "stream": stream,
        "messages": [{"role": "user", "content": "x"}]})


def _data_lines(text):
    return [l[len("data: "):] for l in text.split("\n") if l.startswith("data:")]


def test_each_chunk_carries_when_the_engine_produced_it(srv, engine):
    r = _post(srv, stream=True)
    assert r.status_code == 200
    lines = _data_lines(r.text)
    chunks = [json.loads(l) for l in lines if l.startswith("{")]
    assert len(chunks) == 4
    t = [c["harmony"]["t_ms"] for c in chunks]
    assert all(isinstance(x, int) for x in t)
    assert t == sorted(t)                                    # non-decreasing
    # Engine delays: a @0, b @+300, c completes @+600 more (split line), usage @0 more.
    tol = 150
    assert abs((t[1] - t[0]) - 300) < tol
    assert abs((t[2] - t[1]) - 600) < tol
    assert t[3] - t[2] < tol
    # The original content is intact around the stamp.
    assert chunks[0]["choices"][0]["delta"]["content"] == "a"
    assert chunks[1]["x"] == 1
    assert chunks[2]["choices"][0]["delta"]["content"] == "c"
    # An existing `harmony` object is merged into, not replaced.
    assert chunks[2]["harmony"] == {"node": "n1", "t_ms": t[2]}
    # [DONE], non-JSON data and comments pass byte-for-byte.
    assert "data: [DONE]\n\n" in r.text
    assert "data: not json\n\n" in r.text
    assert ": keepalive\n\n" in r.text
    # SSE framing is kept: every event still ends in a blank line.
    assert r.text.endswith("data: [DONE]\n\n")
    assert r.text.count("\n\n") == len(SCRIPT) - 1 + 1       # 6 events + the keepalive


def test_a_non_streamed_response_is_unchanged(srv, engine):
    r = _post(srv, stream=False)
    assert r.status_code == 200
    assert r.content == NON_STREAM


def test_a_relay_of_another_node_keeps_the_serving_nodes_stamp(srv, engine, monkeypatch):
    # A same-host peer holds the unit: this node forwards and must not re-stamp,
    # since the peer (here: the stub) is the node that read the engine.
    monkeypatch.setattr(srv, "_held_on_host", lambda name: engine)
    r = _post(srv, stream=True)
    assert r.status_code == 200
    chunks = [json.loads(l) for l in _data_lines(r.text) if l.startswith("{")]
    assert [c.get("harmony") for c in chunks] == [None, None, {"node": "n1"}, None]


def test_the_stamper_on_its_own(srv):
    now = [10.0]
    s = srv.SseStamper(10.0, clock=lambda: now[0])
    assert s.feed(b'data: {"a":1}\r\n\r\ndata: {"b"') == b'data: {"a":1,"harmony":{"t_ms":0}}\r\n\r\n'
    now[0] = 10.25
    assert s.feed(b':2}\n\ndata: [DONE]\n\n') == \
        b'data: {"b":2,"harmony":{"t_ms":250}}\n\ndata: [DONE]\n\n'
    assert s.feed(b'data: [1,2]\n') == b'data: [1,2]\n'          # JSON, not an object
    assert s.feed(b'data: {"c":"\xe4\xb8\xad"}') == b""          # no newline yet
    now[0] = 10.5
    assert s.flush() == 'data: {"c":"中","harmony":{"t_ms":500}}'.encode()
