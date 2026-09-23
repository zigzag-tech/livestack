"""A port answered by another node's engine is not ours to "load" -- or evict for.

2026-09-22, xc-tower-ubuntu: two harmony-llm nodes read one units file with no
HARMONY_LLM_PORT_OFFSET, so each unit had the same port on both. An embedding
request reached the GPU-1 node, whose peer lookup skipped the (busy, `suspect`)
GPU-0 node that held `embed_multi`. It ensured the embedder locally, coload-off
evicted its resident 27B, and its readiness poll was answered at once by GPU-0's
engine on the shared port. Titles and the classifier were down 13 minutes.
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    units = tmp_path_factory.mktemp("units") / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_title", "model": "m/27b", "port": 8189, "footprint_gb": 21,
         "attributes": {"class": "llm", "params_b": 27}},
        {"name": "embed_multi", "model": "m/embed", "port": 8210, "footprint_gb": 3,
         "extra_args": "--runner pooling", "attributes": {"multilingual": True}},
    ]))
    os.environ["HARMONY_LLM_UNITS_FILE"] = str(units)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_foreign", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_foreign"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:                      # vLLM/torch absent in CI
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


class _Proc:
    def __init__(self, alive=True):
        self.alive = alive

    def poll(self):
        return None if self.alive else 0


def test_a_listener_we_did_not_start_is_foreign(srv, monkeypatch):
    monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": True)
    monkeypatch.setattr(srv, "_procs", {})
    assert srv._foreign_listener("embed_multi")


def test_our_own_live_engine_is_not_foreign(srv, monkeypatch):
    monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": True)
    monkeypatch.setattr(srv, "_procs", {"embed_multi": _Proc(alive=True)})
    assert not srv._foreign_listener("embed_multi")


def test_our_dead_engine_with_someone_else_on_the_port_is_foreign(srv, monkeypatch):
    monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": True)
    monkeypatch.setattr(srv, "_procs", {"embed_multi": _Proc(alive=False)})
    assert srv._foreign_listener("embed_multi")


def test_nothing_listening_is_not_foreign(srv, monkeypatch):
    monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": False)
    monkeypatch.setattr(srv, "_procs", {})
    assert not srv._foreign_listener("embed_multi")


def test_load_refuses_a_foreign_port_without_spawning(srv, monkeypatch):
    monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": True)
    monkeypatch.setattr(srv, "_procs", {})

    def no_spawn(*a, **k):
        raise AssertionError("must not start a second engine on a served port")
    monkeypatch.setattr(srv.subprocess, "Popen", no_spawn)
    with pytest.raises(RuntimeError, match="HARMONY_LLM_PORT_OFFSET"):
        srv._load("embed_multi")


def test_a_suspect_peer_is_still_asked_and_a_mia_one_is_not(srv, monkeypatch):
    rows = [
        {"host_id": "busy-holder", "state": "suspect", "kinds": ["llm"], "peer": "http://h:8190/livestack"},
        {"host_id": "gone", "state": "mia", "kinds": ["llm"], "peer": "http://h:8192/livestack"},
        {"host_id": "fresh-one", "state": "fresh", "kinds": ["llm"], "peer": "http://h:8194/livestack"},
    ]

    class R:
        def json(self):
            return rows
    monkeypatch.setattr(srv, "BROKER_URLS", ["http://broker"])
    monkeypatch.setattr(srv.httpx, "get", lambda url, timeout=3.0: R())
    hosts = [h for h, _ in srv._same_kind_peers()]
    assert "busy-holder" in hosts and "fresh-one" in hosts and "gone" not in hosts
