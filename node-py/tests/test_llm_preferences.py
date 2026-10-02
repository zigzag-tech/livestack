"""`prefer` orders the survivors — it never swaps, and it never invents.

Three rules from design §4b.3:

* ORDERING AMONG SURVIVORS: `prefer llm.params_b: max` picks the bigger unit
  when the choice is open, and only among units that already satisfied the hard
  requirement.
* NO SWAP ON A PREFERENCE ALONE: a resident unit that satisfies the request
  keeps answering even when a preferred one exists — an eviction and reload of
  a 27B is ~50 s, and a stated preference is not a stated need. (A preference
  with a TIME BUDGET is the swap case, blocked on design Q4 — not here.)
* RECEIPT RECORDED: the selection record carries `preference_key`'s receipt —
  what matched, what was comparable, what was silent — so a reader can see WHY
  a unit won.

A metric nobody measured is ABSENT from the inventory and orders nothing.
"""
import importlib.util
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from selection import unit_inventory

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"

PREFER_MAX_PARAMS = [{"metric": "llm.params_b", "direction": "max"}]


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("prefer")
    units = root / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_title", "model": "m/27b", "port": 8189,
         "footprint_gb": 21, "max_model_len": "24576", "default": True,
         "extra_args": "--max-num-seqs 32 --reasoning-parser qwen3",
         "attributes": {"class": "llm", "params_b": 27, "context_len": 24576}},
        {"name": "flash_next", "model": "m/125b", "port": 8191,
         "footprint_gb": 22, "max_model_len": "131072",
         "extra_args": "--parallel 4", "engine": "strata",
         "attributes": {"class": "llm", "params_b": 125, "context_len": 131072}},
    ]))
    os.environ.update({
        "HARMONY_LLM_UNITS_FILE": str(units),
        "HARMONY_DEMAND_LOG_AGE_DAYS": "21",
        "HARMONY_DEMAND_LOG_DIR": str(root / "demand"),
        "HARMONY_UNIT_COSTS_FILE": str(root / "unit-costs.jsonl"),
    })
    spec = importlib.util.spec_from_file_location("harmony_llm_server_prefer", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_prefer"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


class _Upstream(BaseHTTPRequestHandler):
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
        out = json.dumps({"model": body.get("model"),
                          "choices": [{"message": {"content": "4"}}],
                          "usage": {"prompt_tokens": 3, "completion_tokens": 1}}).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


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
    monkeypatch.setattr(srv, "_vllm_up", lambda **k: True)
    yield base
    httpd.shutdown()


def _demand_rows(srv):
    srv.DEMAND.flush()
    path = Path(srv.DEMAND.path)
    return [json.loads(l) for l in path.read_text().splitlines()] if path.exists() else []


def test_ordering_among_survivors_follows_the_preference(srv):
    # With the choice open, `params_b: max` orders the 125B first...
    assert srv._ordered(PREFER_MAX_PARAMS)[0] == "flash_next"
    # ...and a preference for the minimum orders the other way.
    assert srv._ordered([{"metric": "llm.params_b", "direction": "min"}])[0] == "llm_title"
    # No preference: the stable default order (the `default: true` unit first).
    assert srv._ordered([])[0] == "llm_title"


def test_the_preference_orders_only_the_survivors(srv):
    # A requirement the 27B cannot meet leaves it OUT — `prefer` cannot put it
    # back. (Ordering is applied to what satisfied the hard requirement.)
    want = {"class": "llm", "context_len>=": 100000}
    survivors = [n for n in srv.SPECS if srv._local_satisfies(n, want)]
    assert survivors == ["flash_next"]


def test_a_preference_alone_never_swaps_a_resident_unit(srv, upstream, monkeypatch):
    from fastapi.testclient import TestClient
    admitted = []
    monkeypatch.setattr(srv, "admit", lambda *a, **k: admitted.append(k) or
                       {"kind": "flash_next", "granted": True,
                        "device_id": srv.DEVICE_ID_SELF})
    monkeypatch.setattr(type(srv.manager), "resident",
                        property(lambda self: {"llm_title"}))
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm",
                          "harmony_prefer": PREFER_MAX_PARAMS,
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    # llm_title answers (it is resident and satisfies the requirement) and
    # NOTHING was admitted or evicted for the preferred 125B: a preference is
    # not a need.
    assert _Upstream.seen[-1]["model"] == "m/27b"
    assert admitted == []
    assert {"llm_title"} <= set(srv.manager.resident)


def test_the_selection_record_carries_the_preference_receipt(srv, upstream, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setattr(srv, "admit", lambda *a, **k: {"kind": "flash_next",
                                                        "granted": True,
                                                        "device_id": srv.DEVICE_ID_SELF})
    before = len(_demand_rows(srv))
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm",
                          "harmony_prefer": PREFER_MAX_PARAMS,
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 200
    rows = _demand_rows(srv)[before:]
    assert rows and rows[-1]["unit"] == "flash_next"
    receipt = rows[-1].get("preference_receipt")
    assert receipt, "the selection record must carry the preference receipt"
    clause = receipt[0]["clause"]
    assert clause["metric"] == "llm.params_b" and clause["direction"] == "max"
    assert receipt[0]["value"] == 125.0          # why flash_next won, in numbers
    assert receipt[0]["comparable"] is True


def test_an_unmeasured_metric_is_no_opinion_not_a_zero(srv):
    # Nothing has measured decode speed for these units: the clause is
    # comparable-by-absence and cannot rank anything.
    _key, receipt = srv._prefer_key("flash_next",
                                    [{"metric": "llm.decode_tok_s", "direction": "max"}])
    assert receipt[0]["value"] is None
    assert receipt[0]["reason"] == "no observation"


def test_a_malformed_preference_is_a_400(srv, upstream):
    from fastapi.testclient import TestClient
    client = TestClient(srv.app)
    r = client.post("/v1/chat/completions",
                    json={"model": "require:class=llm",
                          "harmony_prefer": [{"metric": "llm.nope", "direction": "max"}],
                          "messages": [{"role": "user", "content": "x"}]})
    assert r.status_code == 400


def test_the_inventory_publishes_what_it_measured_with_sample_counts():
    inv = unit_inventory({"params_b": 27, "context_len": 24576},
                         fitted={"decode_tok_s": 41.5, "decode_tok_s_samples": 12},
                         revision="measured:abc")
    assert inv["llm"]["params_b"]["value"] == 27.0
    assert inv["llm"]["decode_tok_s"]["sample_count"] == 12
    assert inv["llm"]["decode_tok_s"]["model_revision"] == "measured:abc"
    # A metric nobody measured is ABSENT — silence is not a zero.
    assert "first_token_ms" not in inv["llm"]
    assert unit_inventory({}, fitted=None) == {}
