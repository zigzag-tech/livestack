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
