"""A stop that arrives while an engine is loading must not wait out the load.

2026-10-07, xc-tower-ubuntu: a reboot a minute after boot found harmony-llm in
the middle of starting vLLM. systemd's SIGTERM reached the engine while it was
still initialising, where it was ignored; `_load` kept polling for the port
(up to HARMONY_LLM_START_TIMEOUT, 900 s) from a request thread, so uvicorn's
graceful shutdown waited for it and systemd ran out the full 90 s stop timeout
and SIGKILLed the unit. `_load` has to notice the shutdown, kill the engine it
started and give up -- without recording a start failure, which would put a
healthy unit on cooldown for a stop that was not its fault.
"""
import importlib.util
import json
import sys
import threading
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"

# An engine that is "still initialising": ignores SIGTERM and never serves.
_STUBBORN = ("import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
             "time.sleep(120)")


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    units = tmp_path_factory.mktemp("units") / "units.json"
    units.write_text(json.dumps([
        {"name": "llm_title", "model": "m/8b", "port": 8189, "footprint_gb": 8,
         "attributes": {"class": "llm", "params_b": 8}},
    ]))
    os.environ["HARMONY_LLM_UNITS_FILE"] = str(units)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_shutdown", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_shutdown"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:                      # vLLM/torch absent in CI
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


class _StubbornEngine:
    name = "stubborn"

    def env(self, spec, base_env):
        return dict(base_env)

    def argv(self, spec, budget=None):
        return [sys.executable, "-c", _STUBBORN]

    def capture(self):
        return None

    def stop(self, proc):
        proc.kill()
        proc.wait(timeout=10)


def test_shutdown_during_load_kills_the_engine_and_gives_up(srv, monkeypatch):
    monkeypatch.setattr(srv, "_engine_for", lambda spec: _StubbornEngine())
    monkeypatch.setattr(srv, "_vllm_up", lambda timeout=2.0, name="": False)
    monkeypatch.setattr(srv, "_tee_engine_output", lambda proc, capture: None)
    monkeypatch.setattr(srv, "_procs", {})
    monkeypatch.setattr(srv, "_START_FAILURES", {})
    srv._SHUTDOWN.clear()

    outcome = {}

    def run():
        try:
            srv._load("llm_title")
            outcome["result"] = "returned"
        except Exception as e:
            outcome["error"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    time.sleep(1.0)                              # the engine is up and "loading"
    assert t.is_alive() and "llm_title" in srv._procs
    engine = srv._procs["llm_title"]

    srv._SHUTDOWN.set()
    try:
        t.join(timeout=10)
        assert not t.is_alive(), "_load kept polling after shutdown began"
        assert isinstance(outcome.get("error"), RuntimeError)
        assert "shutting down" in str(outcome["error"])
        assert engine.poll() is not None, "the engine it started is still running"
        assert "llm_title" not in srv._procs
        assert "llm_title" not in srv._START_FAILURES
    finally:
        srv._SHUTDOWN.clear()
        if engine.poll() is None:
            engine.kill()
