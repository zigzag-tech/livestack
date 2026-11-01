"""A request waiting on the broker must not freeze every other request.

Measured on zz-whitby 2026-10-18 with Flash-Next at `max_concurrent: 2`: a third
request found the unit saturated and went to `admit()`. That call is blocking
HTTP, and it ran ON the node's event loop. The host broker, planning it, probes
this same node's `/livestack/residence` — which could not answer, because the
loop was blocked waiting on the broker. The probe timed out after 5.0 s
(`saturated` at 02:03:40.757, `/admit` answered 02:03:45.773), and for those
five seconds the two streams already decoding were not read: their TCP buffers
filled, the engine blocked on writing, and decoding stopped. Each burst of
three paid ~5 s on its two running requests.

So every blocking network call on the request path (admission, the same-host
holder lookup, the fleet view) runs off the loop, and this pins it: while one
request sits in a slow admission, `/health` on the same node still answers.
"""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("loop")
    units = root / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_title", "model": "m/27b", "port": 8189, "footprint_gb": 21,
         "max_model_len": "24576", "default": True,
         "attributes": {"class": "llm", "params_b": 27, "context_len": 24576}},
        {"name": "flash_next", "model": "m/125b", "port": 8191, "footprint_gb": 21,
         "engine": "strata",
         "attributes": {"class": "llm", "params_b": 125, "context_len": 131072}},
    ]))
    os.environ.update({
        "HARMONY_LLM_UNITS_FILE": str(units),
        "HARMONY_UNIT_COSTS_FILE": str(root / "unit-costs.jsonl"),
        "HARMONY_LLM_WARM_ON_START": "0",
    })
    spec = importlib.util.spec_from_file_location("harmony_llm_server_loop", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_loop"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


def test_a_slow_admission_does_not_block_the_node(srv, monkeypatch):
    from fastapi.testclient import TestClient

    def slow_admit(*a, **k):
        time.sleep(1.5)                      # the broker, waiting on its probe
        return {"kind": "flash_next", "granted": False, "reason": "test: refused"}

    monkeypatch.setattr(srv, "admit", slow_admit)
    monkeypatch.setattr(srv, "_held_elsewhere", lambda name: None)
    monkeypatch.setattr(srv, "_foreign_listener", lambda name: False)
    monkeypatch.setattr(type(srv.manager), "resident", property(lambda self: set()))
    done = {}
    # ONE event loop for both requests, as in the real server: a TestClient
    # outside `with` starts a fresh loop per request and cannot show a block.
    with TestClient(srv.app) as client:
        def ask():
            done["status"] = client.post("/v1/chat/completions", json={
                "model": "require:class=llm,context_len>=100000",
                "messages": [{"role": "user", "content": "x"}]}).status_code

        t = threading.Thread(target=ask)
        t.start()
        time.sleep(0.3)                      # the request is now inside admit()
        t0 = time.time()
        assert client.get("/health").status_code == 200
        waited = time.time() - t0
        t.join()
    assert done["status"] == 503             # the refusal still reaches its caller
    assert waited < 0.5, f"/health waited {waited:.2f}s behind a blocked admission"


def test_the_measurement_is_handed_the_engine_process_it_started(srv, monkeypatch):
    # `_record_measurement` passed pid=None, so an engine that reads its cost
    # off the process (Strata) could only ever answer `unknown` — which the
    # planner charges as the whole device.
    import subprocess as sp
    seen = {}

    class _Eng:
        def measure(self, spec, capture, pid, argv):
            seen["pid"] = pid
            return {"measured": "unknown", "unmatched": ["test"]}

    proc = sp.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        monkeypatch.setitem(srv._procs, "flash_next", proc)
        monkeypatch.setattr(srv, "_engine_for", lambda spec: _Eng())
        srv._record_measurement("flash_next", srv.SPECS["flash_next"], [], None)
        assert seen["pid"] == proc.pid
    finally:
        proc.kill()


def test_a_fleet_routed_request_travels_as_asked(srv, monkeypatch):
    # Asked from a node that cannot serve it, a speed-preferring request is
    # ranked over the fleet and forwarded: the body as the caller wrote it (the
    # receiver resolves the requirement), the routed header set (it never
    # re-routes), and no place held in a local queue for work served elsewhere.
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from fastapi.testclient import TestClient
    got = {}

    class _Peer(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("content-length") or 0)
            got["body"] = json.loads(self.rfile.read(n))
            got["routed"] = self.headers.get("x-harmony-routed")
            out = json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

    peer = HTTPServer(("127.0.0.1", 0), _Peer)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{peer.server_address[1]}"
    view = {"generated_at": time.time(), "hosts": {"other": {"nodes": [{
        "peer": f"{base}/livestack", "state": "fresh", "ready": True, "kinds": ["llm"],
        "units": [{"kind": "big", "resident": True,
                   "attributes": {"class": "llm", "params_b": 400, "context_len": 8192}}]}]}}}
    monkeypatch.setattr(srv, "FLEET_URL", "http://fleet.invalid:8801")
    monkeypatch.setattr(srv, "_fleet_view", lambda url: view)
    monkeypatch.setattr(srv, "_held_elsewhere", lambda name: None)
    acquired = []
    monkeypatch.setattr(srv._QUEUES, "acquire", lambda *a, **k: acquired.append(a))
    body = {"model": "require:class=llm,params_b>=300",
            "harmony_prefer": [{"metric": "llm.decode_tok_s", "direction": "max"}],
            "messages": [{"role": "user", "content": "x"}]}
    try:
        with TestClient(srv.app) as client:
            r = client.post("/v1/chat/completions", json=body)
        assert r.status_code == 200, r.text
        assert got["body"]["model"] == "require:class=llm,params_b>=300"
        assert got["routed"]
        assert acquired == []
    finally:
        peer.shutdown()


def test_a_load_refused_for_host_memory_keeps_asking_while_attempts_drain(srv, monkeypatch):
    # The broker wrote the load WAITING on the host ledger; asking again is what
    # keeps that alive while task attempts finish (host_ledger.py, "drain,
    # don't kill"). A refusal for any other reason is answered at once.
    from fastapi.testclient import TestClient
    calls = []

    def admit(*a, **k):
        calls.append(time.time())
        if len(calls) <= 2:
            return {"granted": False, "kind": "flash_next",
                    "reason": "the planner could not place it on any device",
                    "defer_reason": "insufficient host memory: need 45 GiB > free 30 GiB"}
        return {"granted": False, "kind": "flash_next", "reason": "test: refused for another reason"}

    monkeypatch.setattr(srv, "admit", admit)
    monkeypatch.setattr(srv, "DRAIN_POLL_S", 0.01)
    monkeypatch.setattr(srv, "_held_elsewhere", lambda name: None)
    monkeypatch.setattr(srv, "_foreign_listener", lambda name: False)
    monkeypatch.setattr(type(srv.manager), "resident", property(lambda self: set()))
    with TestClient(srv.app) as client:
        r = client.post("/v1/chat/completions", json={
            "model": "require:class=llm,context_len>=100000",
            "messages": [{"role": "user", "content": "x"}]})
    assert len(calls) == 3                 # two host-memory defers waited out, then answered
    assert r.status_code == 503 and "another reason" in r.text


def test_the_drain_wait_is_bounded(srv, monkeypatch):
    from fastapi.testclient import TestClient
    calls = []
    monkeypatch.setattr(srv, "admit", lambda *a, **k: calls.append(1) or {
        "granted": False, "kind": "flash_next", "reason": "no room",
        "defer_reason": "insufficient host memory: need 45 GiB > free 1 GiB"})
    monkeypatch.setattr(srv, "DRAIN_POLL_S", 0.01)
    monkeypatch.setattr(srv, "DRAIN_WAIT_S", 0.2)
    monkeypatch.setattr(srv, "_held_elsewhere", lambda name: None)
    monkeypatch.setattr(srv, "_foreign_listener", lambda name: False)
    monkeypatch.setattr(type(srv.manager), "resident", property(lambda self: set()))
    t0 = time.time()
    with TestClient(srv.app) as client:
        r = client.post("/v1/chat/completions", json={
            "model": "require:class=llm,context_len>=100000",
            "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 503 and "host memory" in r.text   # the reason reaches the caller
    assert 1 < len(calls) and time.time() - t0 < 5
