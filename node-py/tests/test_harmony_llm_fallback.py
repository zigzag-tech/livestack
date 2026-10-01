"""harmony-llm's "broker forgot" fallback respects a real refusal, and a failed
engine start is not respawned on every request.

2026-09-30 05:29-15:42: the broker refused llm_general ("no device can fit even
with preemption"; an image model held the card). The fallback loaded it anyway,
the start failed, the in-flight load withheld this node's registration, the
broker then said "no unit satisfies", and the fallback fired again: 346 doomed
starts, the 27B down ten hours."""
import importlib.util
import json
import sys
import time
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("fb")
    units = root / "units.json"
    units.write_text(json.dumps([{"name": "llm_general", "model": "m", "port": 8189,
                                  "footprint_gb": 21, "attributes": {"class": "llm", "params_b": 27}}]))
    os.environ.update({"HARMONY_LLM_UNITS_FILE": str(units), "HARMONY_UNIT_COSTS_FILE": str(root / "c.jsonl")})
    os.environ.pop("HARMONY_DEMAND_LOG_AGE_DAYS", None)
    spec = importlib.util.spec_from_file_location("harmony_llm_server_fb", HERE)
    m = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_fb"] = m
    try:
        spec.loader.exec_module(m)
    except Exception as e:
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return m


def test_a_broker_that_knows_the_unit_and_refuses_is_final(srv):
    # Exactly what the broker returned at 05:30:44 (plan summary carries the reason).
    refused = {"granted": False, "kind": None, "reason": "the planner could not place it on any device",
               "plan": "defer llm-1790... (no device can fit even with preemption)"}
    assert srv._broker_did_not_know(refused) is False
    assert srv._broker_did_not_know({**refused, "plan": "defer r (residency floor: x loaded 3s ago)"}) is False


def test_a_broker_that_does_not_know_the_unit_allows_the_local_fallback(srv):
    assert srv._broker_did_not_know(
        {"granted": False, "plan": "defer r (no unit satisfies {'class': 'llm'})"}) is True
    assert srv._broker_did_not_know({"granted": False, "defer_reason": "no unit satisfies {}"}) is True


def test_no_reason_reads_as_a_refusal(srv):
    assert srv._broker_did_not_know({"granted": False}) is False


class _DeadOnArrival:
    """A vLLM that exits during startup, as it does when the card is full."""
    spawned = 0

    def __init__(self, *a, **k):
        type(self).spawned += 1
        self.returncode = 1
        self.pid = 0
        self.stdout = iter(())

    def poll(self):
        return 1


def test_a_failed_start_is_not_respawned_by_the_next_request(srv, monkeypatch):
    monkeypatch.setattr(srv.subprocess, "Popen", _DeadOnArrival)
    monkeypatch.setattr(srv, "_vllm_up", lambda **k: False)
    monkeypatch.setattr(srv, "_foreign_listener", lambda name: False)
    monkeypatch.setattr(srv, "_tee_engine_output", lambda proc, cap: None)
    srv._START_FAILURES.clear()
    _DeadOnArrival.spawned = 0
    with pytest.raises(RuntimeError, match="exited during startup"):
        srv._load("llm_general")
    for _ in range(5):                                   # five more requests
        with pytest.raises(RuntimeError, match="not retrying"):
            srv._load("llm_general")
    assert _DeadOnArrival.spawned == 1
    fails, not_before, _ = srv._START_FAILURES["llm_general"]
    assert fails == 1 and 25 < not_before - time.time() <= 30
    # A broker grant (it names a device) has had room made: it may try now.
    with pytest.raises(RuntimeError, match="exited during startup"):
        srv._load("llm_general", device=srv.DEVICE_ID_SELF or "dev")
    assert _DeadOnArrival.spawned == 2
    fails, not_before, _ = srv._START_FAILURES["llm_general"]
    assert fails == 2 and 55 < not_before - time.time() <= 60      # doubles

