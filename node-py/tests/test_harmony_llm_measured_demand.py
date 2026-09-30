"""harmony-llm reports what its engine SAID it costs, and records what it served.

Two things the 2026-09-28 recomposition of `llm_general` could not see: the unit
declared 21 GB while the engine reported ~23.5 GiB all-in, and nothing recorded
which adapter each request used. The engine output here is the real vLLM 0.28.0
journal from that day; the upstream is a stub that answers like vLLM does.
"""
import importlib.util
import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"
FIX = Path(__file__).resolve().parent / "fixtures" / "vllm_startup"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("harmony")
    for name in ("chips", "jemm"):
        d = root / name
        d.mkdir()
        (d / "adapter_config.json").write_text(json.dumps({"r": 16}))
    units = root / "units.json"
    units.write_text(json.dumps([{
        "name": "llm_general", "model": "dbirks/Qwen3.8-27B-W4A16-AutoRound",
        "port": 8189, "footprint_gb": 21, "max_model_len": "24576",
        "extra_args": "--max-num-seqs 32 --kv-cache-dtype fp8",
        "adapters": {"chips": str(root / "chips"), "jemm": str(root / "jemm")},
        "lora_base": "Qwen/Qwen3.8-27B",
        "attributes": {"class": "llm", "family": "qwen", "params_b": 27}}]))
    os.environ.update({
        "HARMONY_LLM_UNITS_FILE": str(units),
        "HARMONY_DEMAND_LOG_AGE_DAYS": "21",
        "HARMONY_DEMAND_LOG_DIR": str(root / "demand"),
        "HARMONY_UNIT_COSTS_FILE": str(root / "unit-costs.jsonl"),
        "HARMONY_ADAPTER_DIR": str(root),
        "HARMONY_KV_DTYPES": "auto,fp8",
    })
    spec = importlib.util.spec_from_file_location("harmony_llm_server_md", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_md"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:                      # shared_py/vLLM absent in this venv
        pytest.skip(f"harmony-llm server not importable here: {e}")
    module._test_root = root
    return module


def _engine(srv, fixture_lines):
    """A real subprocess printing engine output, run through the real tee."""
    proc = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdout.write(sys.stdin.read())"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    cap = srv.StartupCapture()
    t = threading.Thread(target=srv._tee_engine_output, args=(proc, cap))
    t.start()
    proc.stdin.write("\n".join(fixture_lines) + "\n")
    proc.stdin.close()
    t.join(5)
    proc.wait(5)
    return cap


def _cmd(srv):
    s = srv.SPECS["llm_general"]
    return (["vllm", "serve", s["model"], "--max-model-len", s["max_model_len"]]
            + s["extra_args"] + srv._lora_launch_args(s))


def test_measured_cost_replaces_the_declared_footprint(srv):
    cap = _engine(srv, (FIX / "v0.28.0-llm_general-fp8.log").read_text().splitlines())
    srv._record_measurement("llm_general", srv.SPECS["llm_general"], _cmd(srv), cap)
    unit = srv._UNITS["llm_general"]
    assert unit.measured_cost["kv_tokens"] == 37981
    assert unit.measured_cost["source"] == "vllm-startup"
    # Reported, not adopted for admission (design §8b): the declared prior stays.
    assert unit.footprint == 21 * (1 << 30)
    assert unit.measured_cost["footprint"] > 23 * (1 << 30)
    # The least it can run with: KV for one 24,576-token request, not the whole pool.
    assert 22.5 * (1 << 30) < unit.measured_cost["min_footprint"] < unit.measured_cost["footprint"]
    rows = srv._COSTS.load()
    chash = srv._COMPOSITION["llm_general"]
    assert rows[chash]["composition"]["kv_dtype"] == "fp8"
    assert rows[chash]["composition"]["adapters"] == [["chips", 16], ["jemm", 16]]


def test_unparsed_engine_report_is_unknown_not_the_prior(srv):
    lines = [l for l in (FIX / "v0.28.0-llm_general-fp8.log").read_text().splitlines()
             if "Actual usage" not in l]
    srv._UNITS["llm_general"].footprint = 21 * (1 << 30)
    cap = _engine(srv, lines)
    import time
    t0 = time.time()
    srv._record_measurement("llm_general", srv.SPECS["llm_general"], _cmd(srv), cap)
    unit = srv._UNITS["llm_general"]
    assert unit.measured_cost["measured"] == "unknown"
    assert unit.measured_cost["unmatched"] == ["actual_usage"]
    assert time.time() - t0 < 15


class _Upstream(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("content-length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        _Upstream.seen.append(body)
        stream = body.get("stream")
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        if stream:
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n')
        else:
            self.wfile.write(json.dumps({"model": body.get("model"), "choices": [{"message": {"content": "A"}}],
                                         "usage": {"prompt_tokens": 1423, "completion_tokens": 1}}).encode())


@pytest.fixture()
def upstream(srv, monkeypatch):
    _Upstream.seen = []
    httpd = HTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    monkeypatch.setattr(srv, "_base_of", lambda name: base)
    monkeypatch.setattr(srv, "_held_elsewhere", lambda name: None)
    monkeypatch.setattr(srv, "_foreign_listener", lambda name: False)
    monkeypatch.setattr(srv.manager, "ensure", lambda *a, **k: None)
    monkeypatch.setattr(srv, "admit", lambda *a, **k: {"kind": "llm_general", "granted": True,
                                                        "device_id": srv.DEVICE_ID_SELF})
    yield base
    httpd.shutdown()


def _demand_rows(srv):
    srv.DEMAND.flush()
    path = Path(srv.DEMAND.path)
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_every_forwarded_request_leaves_one_record(srv, upstream):
    from fastapi.testclient import TestClient
    client = TestClient(srv.app)
    before = len(_demand_rows(srv))
    r = client.post("/v1/chat/completions",
                    headers={"x-harmony-owner": "benchday:acct_123"},
                    json={"model": "jemm", "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    assert _Upstream.seen[-1]["model"] == "jemm"
    r = client.post("/v1/chat/completions", json={"model": "llm_general", "stream": True,
                                                  "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    rows = _demand_rows(srv)[before:]
    assert len(rows) == 2
    adapter_row, stream_row = rows
    assert adapter_row["adapter"] == "jemm" and adapter_row["unit"] == "llm_general"
    assert adapter_row["prompt_tokens"] == 1423 and adapter_row["completion_tokens"] == 1
    assert adapter_row["owner_ns"] == "benchday:" and adapter_row["outcome"] == "ok"
    assert adapter_row["n"] == 1                    # absent in the body: the default, known
    assert "acct_123" not in json.dumps(rows)
    # A stream that carried no usage records null, never 0.
    assert stream_row["adapter"] is None
    assert stream_row["prompt_tokens"] is None and stream_row["completion_tokens"] is None


def test_the_log_status_is_on_residence(srv):
    rep = srv._UNITS["llm_general"].extra_report()
    assert rep["demand_log"]["enabled"] is True
    assert "dropped" in rep["demand_log"]


def test_composition_facts_are_what_the_composer_reads(srv, upstream):
    from fastapi.testclient import TestClient
    client = TestClient(srv.app)
    client.post("/v1/chat/completions", json={"model": "jemm", "messages": []})
    f = client.get("/composition/facts", params={"since": 0}).json()
    u = f["units"][0]
    assert u["name"] == "llm_general" and u["lora_base"] == "Qwen/Qwen3.8-27B"
    assert u["adapters"] == {"chips": 16, "jemm": 16} and u["kv_dtype"] == "fp8"
    assert u["max_model_len"] == 24576 and u["max_num_seqs"] == 32
    names = {a["name"] for a in f["adapter_catalogue"] if "error" not in a}
    assert {"chips", "jemm"} <= names
    # Directories that are not adapters are named, not silently skipped.
    assert any("error" in a for a in f["adapter_catalogue"])
    assert f["kv_dtypes"] == ["auto", "fp8"]
    assert any(r.get("adapter") == "jemm" for r in f["trace"])
    assert f["demand_log"]["enabled"] is True


def test_a_multi_sample_request_records_its_n(srv, upstream):
    """The hub's chip call sends n=12; vLLM counts twelve requests for it."""
    from fastapi.testclient import TestClient
    client = TestClient(srv.app)
    before = len(_demand_rows(srv))
    client.post("/v1/chat/completions", json={"model": "chips", "n": 12, "messages": []})
    rows = _demand_rows(srv)[before:]
    assert [r["n"] for r in rows] == [12]
