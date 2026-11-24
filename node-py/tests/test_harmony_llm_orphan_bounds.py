"""No generation without a reader, and none without an end (2026-11-24).

Measured on node-a: requests with no max_tokens ran with llama.cpp's n_predict -1, and their callers had
long gone (one had given up while its request still waited in the queue; the node sent it anyway). Two such orphans
held both Inkling slots for over an hour, 34k and 26k tokens, and every other caller timed out behind them.

These are unit tests on purpose: the failure is a TIMING (a caller leaving while the engine has not yet answered a
non-streamed request), which the proxy's integration fixtures, answering at once, cannot show.
"""
import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parents[1] / "examples" / "harmony-llm" / "server.py"


@pytest.fixture(scope="module")
def srv(tmp_path_factory):
    import os
    root = tmp_path_factory.mktemp("orphan")
    units = root / "units.json"
    units.write_text(json.dumps([{"name": "u", "model": "m/8b", "port": 8189, "footprint_gb": 8, "default": True,
                                  "attributes": {"class": "llm", "params_b": 8, "context_len": 8192}}]))
    os.environ.update({"HARMONY_LLM_UNITS_FILE": str(units), "HARMONY_UNIT_COSTS_FILE": str(root / "c.jsonl"),
                       "HARMONY_LLM_WARM_ON_START": "0"})
    spec = importlib.util.spec_from_file_location("harmony_llm_server_orphan", HERE)
    module = importlib.util.module_from_spec(spec)
    sys.modules["harmony_llm_server_orphan"] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        pytest.skip(f"harmony-llm server not importable here: {e}")
    return module


def test_a_completion_without_an_output_bound_gets_one(srv):
    body = json.dumps({"messages": [{"role": "user", "content": "x"}]}).encode()
    out = json.loads(srv._bound_output("chat/completions", json.loads(body), body))
    assert out["max_tokens"] == srv._DEFAULT_MAX_TOKENS


def test_a_named_bound_and_other_paths_are_left_alone(srv):
    named = json.dumps({"max_tokens": 7}).encode()
    assert srv._bound_output("chat/completions", json.loads(named), named) == named
    embed = json.dumps({"input": "x"}).encode()
    assert srv._bound_output("embeddings", json.loads(embed), embed) == embed


class _Caller:
    def __init__(self, leaves_after):
        self.checks, self.leaves_after = 0, leaves_after

    async def is_disconnected(self):
        self.checks += 1
        return self.checks > self.leaves_after


class _SlowEngine:
    """A non-streamed generation: nothing comes back until it is done."""
    cancelled = False

    async def send(self, req, stream=True):
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            _SlowEngine.cancelled = True
            raise


def test_the_upstream_request_is_cancelled_when_its_caller_leaves(srv):
    async def go():
        with pytest.raises(srv._CallerGone):
            await asyncio.wait_for(srv._send_while_caller_waits(_SlowEngine(), object(), _Caller(leaves_after=1)), 10)
    asyncio.run(go())
    assert _SlowEngine.cancelled


def test_a_request_whose_caller_waits_is_answered(srv):
    class _Quick:
        async def send(self, req, stream=True):
            await asyncio.sleep(0.05)
            return "response"
    assert asyncio.run(srv._send_while_caller_waits(_Quick(), object(), _Caller(leaves_after=99))) == "response"
