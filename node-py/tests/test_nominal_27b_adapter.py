"""Real HTTP boundaries, not qualification of a model's extraction quality."""
import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import httpx
import pytest

from livestack_node.decisions.admission import Admission
from livestack_node.decisions.contract import ContractError
from livestack_node.decisions.nominal_27b_adapter import QualifiedEngine, Nominal27BExtractionAdapter
from test_pane_ask_extraction_contract import extraction_request


@pytest.fixture
def engine_http():
    state = {"calls": [], "content": {"outcome": "ask", "request_quote": "Proceed?",
             "kind": "confirm", "question": "Proceed?", "cue": "Confirm", "options": []},
             "finish": "stop", "raw": None, "delay": 0, "completed": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            state["calls"].append(json.loads(self.rfile.read(int(self.headers["content-length"]))))
            if state.get("before_generate"):
                state["before_generate"]()
            time.sleep(state["delay"])
            raw = state["raw"]
            if raw is None:
                raw = json.dumps({"choices": [{"finish_reason": state["finish"],
                    "message": {"content": json.dumps(state["content"])}}],
                    "usage": {"prompt_tokens": 42}}).encode()
            self.send_response(200)
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Expected only for the client deadline/size cancellation controls.
            finally:
                state["completed"].set()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    engine = QualifiedEngine("llm-27", f"http://127.0.0.1:{server.server_port}/v1/chat/completions",
        "device-test", "cuda", "fixture-revision", "fixture-tokenizer", "fixture-quality-control",
        "bf16", 27)
    yield engine, state
    server.shutdown()
    server.server_close()
    thread.join()


def request():
    value = extraction_request()
    value["deadline_at_ms"] = int(time.time() * 1000) + 8000
    value["state"]["current_agent_message"] = "I have finished the plan. Proceed?"
    return value


def infer(engine, value=None, grant=None):
    async def run():
        async with httpx.AsyncClient() as client:
            return await Nominal27BExtractionAdapter(engine, client=client).infer(value or request(),
                admission=grant or Admission(True, engine.unit, engine.backend, engine.device_id, True, "lease-test"))
    return asyncio.run(run())


def test_real_http_extraction_preserves_identity_and_fixed_generation_policy(engine_http):
    engine, state = engine_http
    req = request()
    result = infer(engine, req)
    assert result["request_id"] == req["request_id"]
    assert result["evidence_revision"] == req["evidence_revision"]
    assert result["answers"]["ask"]["value"]["request_quote"] == "Proceed?"
    assert result["execution"]["qualification_hash"] == engine.qualification_hash
    assert result["coverage"]["used_tokens"] == 42
    sent = state["calls"][0]
    assert sent["model"] == engine.unit and sent["temperature"] == 0
    assert sent["max_tokens"] == 1024 and sent["stream"] is False
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert "tools" not in sent
    state["content"] = {"outcome": "no_grounded_ask"}
    assert infer(engine)["answers"]["ask"]["value"] == {"outcome": "no_grounded_ask"}


@pytest.mark.parametrize("change", [{"params_b": 4}, {"params_b": 28},
    {"qualification_hash": "unqualified"}, {"model_revision": ""}, {"model_class": "classifier"}])
def test_ineligible_registry_refuses_before_real_engine_call(engine_http, change):
    engine, state = engine_http
    with pytest.raises(ContractError) as error:
        infer(replace(engine, **change))
    assert error.value.cause == "profile_unavailable"
    assert state["calls"] == []


def test_mismatched_admission_cannot_reach_engine(engine_http):
    engine, state = engine_http
    with pytest.raises(ContractError) as error:
        infer(engine, grant=Admission(True, engine.unit, engine.backend, "another-device", True, "lease-test"))
    assert error.value.cause == "forbidden" and state["calls"] == []


@pytest.mark.parametrize("fault", ["ungrounded", "truncated", "malformed", "overflow"])
def test_real_engine_invalid_output_is_a_named_failure(engine_http, fault):
    engine, state = engine_http
    if fault == "ungrounded":
        state["content"]["request_quote"] = "Should I delete everything?"
    elif fault == "truncated":
        state["finish"] = "length"
    elif fault == "malformed":
        state["raw"] = b"not JSON"
    else:
        state["raw"] = b" " * (65536 + 1)
    with pytest.raises(ContractError) as error:
        infer(engine)
    assert error.value.cause == "invalid_output"
    assert len(state["calls"]) == 1


def test_real_engine_deadline_collects_execution_without_fallback(engine_http):
    engine, state = engine_http
    state["delay"] = 0.5
    req = request()
    req["deadline_at_ms"] = int(time.time() * 1000) + 300
    with pytest.raises(ContractError) as error:
        infer(engine, req)
    assert error.value.cause == "deadline_exceeded"
    assert len(state["calls"]) == 1
    assert state["completed"].is_set()
