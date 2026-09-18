import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest

from livestack_node.policy_lab.contracts import ContractError
from livestack_node.policy_lab.profile_worker import run_profile_cell


class _Engine(BaseHTTPRequestHandler):
    busy = False

    def log_message(self, *_args):
        pass

    def do_GET(self):
        if self.path == "/livestack/residence":
            body = {"units": [{"kind": "voxcpm", "busy": self.busy, "resident": True}]}
        elif self.path == "/health":
            body = {"inflight": 0, "model": ["voxcpm"]}
        else:
            self.send_error(404)
            return
        raw = json.dumps(body).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)

    def do_POST(self):
        size = int(self.headers.get("Content-Length", 0))
        self.rfile.read(size)
        raw = b"pcm"
        self.send_response(200); self.send_header("Content-Length", str(len(raw)))
        self.end_headers(); self.wfile.write(raw)


@pytest.fixture
def engine():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Engine)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown(); thread.join(); server.server_close(); _Engine.busy = False


def _cell():
    return {
        "cell_id": "tts:path:w0:text-20-chars:c1:warm:model:runtime:gpu",
        "workload_class": "tts", "requester_vantage": "vantage-a",
        "execution_target": "worker-a", "network_path_id": "path",
        "processing_scopes": ["scope-a"], "observation_window": 0,
        "shape": "text-20-chars", "concurrency": 1, "cache_state": "warm",
        "model_revision": "model", "runtime_revision": "runtime",
        "hardware_revision": "gpu", "sample_target": 2,
    }


def test_profile_worker_collects_bounded_metadata_only_pack(engine):
    pack = run_profile_cell(
        _cell(), {"worker-a:tts": engine},
        protected={"abort_on_active_stream_interference": True},
        voice_id="voice",
    )
    assert pack["kind"] == "measured_profile_pack"
    assert pack["status"] == "complete"
    assert len(pack["samples"]) == 2
    assert all(sample["completion_us"] >= sample["first_output_us"] >= 0 for sample in pack["samples"])
    serialized = json.dumps(pack)
    assert "Profile sentence" not in serialized
    assert "voice" not in serialized


def test_profile_worker_refuses_busy_endpoint_without_requests(engine):
    _Engine.busy = True
    with pytest.raises(ContractError, match="protected endpoint is busy"):
        run_profile_cell(
            _cell(), {"worker-a:tts": engine},
            protected={"abort_on_active_stream_interference": True},
            voice_id="voice",
        )
