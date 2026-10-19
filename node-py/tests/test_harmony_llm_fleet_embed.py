"""An embeddings need no local unit meets goes to the embed node that does.

Before: `require:class=embed,dim=512,model=google/embeddinggemma-2` against the
tower gateway answered `503 nothing satisfies` while a Gemma node was live on
the mesh, because the gateway only ever placed on units its broker could LOAD.
"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

DIR = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm"
sys.path.insert(0, str(DIR))
import fleet_embed as fe  # noqa: E402


def _fleet(base_port, *, ready=True, state="fresh", attrs=None, kinds=("embed",), probe=5.0):
    attrs = attrs or {"class": "embed", "model": "google/embeddinggemma-2", "dim": 512,
                      "multilingual": True, "modality": "text+image"}
    return {"hosts": {"h": {"nodes": [{
        "peer": f"http://127.0.0.1:{base_port}/livestack", "state": state, "kinds": list(kinds),
        "ready": ready, "probe_ms": probe, "units": [{"kind": "embed_x", "attributes": attrs}]}]}}}


def test_a_clause_the_unit_does_not_declare_is_unmet():
    nodes = fe.embed_nodes([_fleet(1)])
    assert fe.pick({"class": "embed", "dim": 512, "multilingual": True}, nodes)
    assert fe.pick({"class": "embed", "dim": 1024}, nodes) is None          # wrong dim
    assert fe.pick({"class": "embed", "family": "gemma"}, nodes) is None    # silence is not a yes
    assert fe.pick({"class": "embed", "dim>=": 256}, nodes)


def test_only_live_ready_embed_nodes_count():
    assert fe.embed_nodes([_fleet(1, ready=False)]) == []
    assert fe.embed_nodes([_fleet(1, state="mia")]) == []
    assert fe.embed_nodes([_fleet(1, kinds=("llm",))]) == []


def test_different_models_are_never_chosen_by_distance():
    other = {"class": "embed", "model": "Xenova/all-MiniLM-L6-v2", "dim": 384}
    nodes = fe.embed_nodes([_fleet(1, probe=40.0), _fleet(2, attrs=other, probe=3.0)])
    with pytest.raises(fe.AmbiguousEmbedder, match="name one with `model="):
        fe.pick({"class": "embed"}, nodes)
    assert fe.pick({"class": "embed", "model": "google/embeddinggemma-2"}, nodes)["base"].endswith(":1")


def test_the_nearest_node_wins_and_one_without_a_probe_sorts_last():
    nodes = fe.embed_nodes([_fleet(1, probe=40.0), _fleet(2, probe=3.0), _fleet(3, probe=None)])
    assert fe.pick({"class": "embed"}, nodes)["base"].endswith(":2")
    assert fe.pick({"class": "embed"}, fe.embed_nodes([_fleet(3, probe=None), _fleet(1, probe=9.0)]))["base"].endswith(":1")


class _Node(BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        _Node.seen.append((self.path, body))
        out = json.dumps({"data": [{"embedding": [0.1] * 4}], "model": body["model"]}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, *a):
        pass


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    units = tmp_path_factory.mktemp("units") / "units.json"
    units.write_text(json.dumps([{"name": "llm_small", "model": "m", "port": 8197, "footprint_gb": 1,
                                  "attributes": {"class": "llm", "params_b": 9}}]))
    import os
    os.environ["HARMONY_LLM_UNITS_FILE"] = str(units)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_fleet", DIR / "server.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_fleet"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


def test_the_gateway_forwards_an_embeddings_need_to_the_fleet_node(srv, monkeypatch):
    from fastapi.testclient import TestClient
    httpd = HTTPServer(("127.0.0.1", 0), _Node)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        port = httpd.server_address[1]
        monkeypatch.setattr(srv, "_fetch_fleets", lambda *_a: [_fleet(port)])
        c = TestClient(srv.app)
        r = c.post("/v1/embeddings", json={"model": "require:class=embed,dim=512,multilingual=true",
                                           "input": ["你好"]})
        assert r.status_code == 200, r.text
        path, body = _Node.seen[-1]
        assert path == "/v1/embeddings"
        assert body["model"] == "google/embeddinggemma-2"      # the node's name, not our require: string
        assert "harmony_requires" not in body
        # a need the node does not meet is the refusal it always was, not a guess
        r = c.post("/v1/embeddings", json={"model": "require:class=embed,dim=1024", "input": ["x"]})
        assert r.status_code == 503 and "nothing satisfies" in r.text
    finally:
        httpd.shutdown()


def test_an_ambiguous_need_is_a_400_that_names_the_models(srv, monkeypatch):
    from fastapi.testclient import TestClient
    other = {"class": "embed", "model": "Xenova/all-MiniLM-L6-v2", "dim": 384}
    monkeypatch.setattr(srv, "_fetch_fleets", lambda *_a: [_fleet(1), _fleet(2, attrs=other)])
    r = TestClient(srv.app).post("/v1/embeddings", json={"model": "require:class=embed", "input": ["x"]})
    assert r.status_code == 400 and "MiniLM" in r.text and "embeddinggemma" in r.text


def test_a_dead_node_is_a_502_that_names_it(srv, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(srv, "_fetch_fleets", lambda *_a: [_fleet(1)])   # nothing listens on :1
    r = TestClient(srv.app).post("/v1/embeddings", json={"model": "require:class=embed", "input": ["x"]})
    assert r.status_code == 502 and "127.0.0.1:1" in r.text


class _Busy(_Node):
    """Sheds with the embed node's own `503 busy` for the first `shed` calls."""
    shed = 0
    calls = 0

    def do_POST(self):
        type(self).calls += 1
        if type(self).calls <= type(self).shed:
            self.rfile.read(int(self.headers["content-length"]))
            out = json.dumps({"error": {"message": "query lane full (4 requests)", "type": "busy"}}).encode()
            self.send_response(503)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        super().do_POST()


def _busy_node(shed):
    _Busy.shed, _Busy.calls = shed, 0
    httpd = HTTPServer(("127.0.0.1", 0), _Busy)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_a_busy_lane_is_queued_at_the_gateway_not_surfaced(srv, monkeypatch):
    from fastapi.testclient import TestClient
    httpd = _busy_node(shed=3)
    try:
        monkeypatch.setattr(srv, "_fetch_fleets", lambda *_a: [_fleet(httpd.server_address[1])])
        r = TestClient(srv.app).post("/v1/embeddings", json={"model": "require:class=embed", "input": ["x"]})
        assert r.status_code == 200, r.text
        assert _Busy.calls == 4
    finally:
        httpd.shutdown()


def test_a_busy_lane_past_the_wait_is_the_nodes_own_503(srv, monkeypatch):
    from fastapi.testclient import TestClient
    httpd = _busy_node(shed=10**6)
    try:
        monkeypatch.setenv("HARMONY_EMBED_BUSY_WAIT_S", "0.3")
        monkeypatch.setattr(srv, "_fetch_fleets", lambda *_a: [_fleet(httpd.server_address[1])])
        r = TestClient(srv.app).post("/v1/embeddings", json={"model": "require:class=embed", "input": ["x"]})
        assert r.status_code == 503 and "query lane full" in r.text
    finally:
        httpd.shutdown()


def test_a_busy_nearest_node_falls_through_to_another_of_the_same_model(srv, monkeypatch):
    from fastapi.testclient import TestClient
    near = _busy_node(shed=10**6)
    far = HTTPServer(("127.0.0.1", 0), _Node)
    threading.Thread(target=far.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(srv, "_fetch_fleets", lambda *_a: [
            _fleet(near.server_address[1], probe=1.0), _fleet(far.server_address[1], probe=9.0)])
        r = TestClient(srv.app).post("/v1/embeddings", json={"model": "require:class=embed", "input": ["x"]})
        assert r.status_code == 200, r.text
    finally:
        near.shutdown()
        far.shutdown()
