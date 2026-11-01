"""The idle sweep and a proxied request's ensure must not deadlock.

2026-11-01, xc-tower-ubuntu: the idle sweep ran `_gpu_call(manager.maybe_evict)`
— node `_lock`, then the manager's `_guard` — while a media-corpus request's
ensure, run on `_ENSURE_POOL` without `_gpu_call`, held `_guard` and waited for
`_lock` inside `_load`. Neither moved again; every later warm/evict parked an
AnyIO worker behind them until all 40 were gone, and `/health` and
`/livestack/*` stopped answering. The fix is one lock order everywhere:
`_lock` (via `_gpu_call`) before `_guard`.
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
    units = tmp_path_factory.mktemp("units") / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_general", "model": "m/8b", "port": 8189, "footprint_gb": 8,
         "attributes": {"class": "llm", "params_b": 8}},
    ]))
    os.environ["HARMONY_LLM_UNITS_FILE"] = str(units)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_lock_order", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_lock_order"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:                      # vLLM/torch absent in CI
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


def test_proxy_ensure_and_idle_sweep_do_not_deadlock(srv, monkeypatch):
    def fake_load(name="", device=None, budget=None):
        with srv._lock:                          # exactly what the real _load does
            return object()
    monkeypatch.setattr(srv, "_load", fake_load)
    monkeypatch.setattr(srv, "_free", lambda name="": None)

    sweep_holds_lock = threading.Event()
    ensure_started = threading.Event()
    done = {}

    def sweep():
        def body():
            sweep_holds_lock.set()
            ensure_started.wait(2)
            time.sleep(0.2)                      # let the ensure reach its lock
            return srv.manager.maybe_evict()
        srv._gpu_call(body)
        done["sweep"] = True

    def ensure():
        sweep_holds_lock.wait(2)
        ensure_started.set()
        try:
            srv._ensure_on_gpu("llm_general")
        finally:
            srv._busy.release()                  # ownership would pass to send()
        done["ensure"] = True

    threads = [threading.Thread(target=sweep, daemon=True),
               threading.Thread(target=ensure, daemon=True)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert done == {"sweep": True, "ensure": True}, \
        "the idle sweep and a proxied ensure deadlocked on _lock/_guard"
    srv._gpu_call(srv.manager.unload_now)

