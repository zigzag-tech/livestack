"""The JEMM template: JEMM's own prompt, routed to the `jemm` adapter, scored
with the calibrated temperature. The default Simple Jev path is untouched
(tests/test_simple_jev.py)."""
import asyncio
import math

import pytest

from livestack_node.decisions import jemm
from livestack_node.decisions.simple_jev import SimpleJevError, classify

STATUS = {"question": "Blocking on the user.", "working": "Working.", "idle": "Nothing."}


def _req(model="require:class=llm,family=qwen,params_b=[20,30)", **q):
    question = {"type": "choice", "instructions": "Classify the pane.", "criteria": STATUS, **q}
    return {"model": model, "state": {"title": "t", "current_pane_screen": "a\nb"},
            "questions": {"status": question}, "options": {"template": "jemm"}}


def _reply(logprobs):
    return {"model": "jemm", "choices": [{"logprobs": {"content": [{"top_logprobs": [
        {"token": t, "logprob": v} for t, v in logprobs.items()]}]}}], "usage": {"prompt_tokens": 900}}


def test_uses_jemms_prompt_and_routes_to_the_adapter():
    calls = []

    async def invoke(body):
        calls.append(body)
        return _reply({"A": -0.05, "B": -3.0, "C": -4.0})

    out = asyncio.run(classify(_req(), invoke))
    body = calls[0]
    assert body["model"] == "require:class=llm,family=qwen,params_b=[20,30),adapter=jemm"
    assert body["messages"][0] == {"role": "system", "content": jemm.SYSTEM}
    user = body["messages"][1]["content"]
    assert user.startswith("State:\ntitle:\nt\ncurrent_pane_screen:\na\nb")
    assert "A) question: Blocking on the user." in user
    assert user.endswith("Answer with exactly one candidate label.")
    assert "continue_final_message" not in body and body["max_tokens"] == 1
    assert body["structured_outputs"] == {"choice": ["A", "B", "C"]}
    assert out["template_version"] == "jemm-v1"
    assert out["calibration"] == jemm.calibration()["id"]
    assert out["answers"]["status"]["choice"] == "question"


def test_a_caller_with_no_opinion_names_the_adapter():
    assert jemm.adapter_model("local", "jemm") == "jemm"
    assert jemm.adapter_model("", "jemm") == "jemm"


def test_probabilities_use_the_calibrated_temperature():
    cal = {"temperature": 2.0, "threshold": 0.99}
    plan = {"labels": ["A", "B"], "answers": ["x", "y"]}
    out = jemm.score(plan, [{"token": "A", "logprob": -0.1}, {"token": "B", "logprob": -2.1}], cal)
    expect = 1 / (1 + math.exp(-(2.0 / 2.0)))
    assert out["probabilities"]["x"] == pytest.approx(expect)
    assert out["undecided"] is True                 # 0.73 < 0.99


def test_only_choice_questions():
    async def invoke(body):
        raise AssertionError("must refuse before calling the model")
    req = _req()
    req["questions"]["status"] = {"type": "score", "instructions": "x", "criteria": ["lo", "hi"]}
    with pytest.raises(SimpleJevError, match="choice questions only"):
        asyncio.run(classify(req, invoke))


def test_state_not_messages():
    async def invoke(body):
        raise AssertionError
    req = _req()
    del req["state"]
    req["messages"] = [{"role": "user", "content": "x"}]
    with pytest.raises(SimpleJevError, match="takes `state`"):
        asyncio.run(classify(req, invoke))


def test_missing_label_fails_closed():
    async def invoke(body):
        return _reply({"A": -0.1, "B": -1.0})
    with pytest.raises(SimpleJevError, match="absent"):
        asyncio.run(classify(_req(), invoke))


def test_shipped_calibration_is_complete():
    cal = jemm.calibration()
    assert {"id", "temperature", "threshold", "source"} <= set(cal)
    assert cal["temperature"] > 0 and 0 < cal["threshold"] < 1


def test_a_shadow_caller_can_ask_to_yield():
    calls = []

    async def invoke(body):
        calls.append(body)
        return _reply({"A": -0.05, "B": -3.0, "C": -4.0})
    req = _req()
    req["options"]["priority"] = 100
    asyncio.run(classify(req, invoke))
    assert calls[0]["priority"] == 100
    calls.clear()
    asyncio.run(classify(_req(), invoke))
    assert "priority" not in calls[0]
