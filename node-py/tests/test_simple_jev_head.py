"""options.head: Simple Jev's readout plus a trained label head (decisions/label_head.py).
Without the option nothing changes (tests/test_simple_jev.py)."""
import asyncio
import base64
import json

import numpy as np
import pytest

from livestack_node.decisions.simple_jev import SimpleJevError, classify

DIM, MODEL = 4, "qwen3.8-flash-next-q2_0"
CRITERIA = {"x": "The first.", "y": "The second.", "z": "The third."}


@pytest.fixture
def heads(tmp_path):
    """A head whose only effect is +5 on label B, whatever the hidden state."""
    path = tmp_path / "h.npz"
    np.savez(path, U=np.zeros((DIM, 26)), c=np.eye(26)[1] * 5.0, mu=np.zeros(DIM), sd=np.ones(DIM),
             meta=json.dumps({"served_model": MODEL, "dim": DIM}))
    return {"general-v1": str(path)}


def _req(qtype="choice", **options):
    criteria = CRITERIA if qtype == "choice" else list(CRITERIA)
    return {"model": "require:class=llm", "state": {"s": "t"},
            "questions": {"q": {"type": qtype, "instructions": "Pick one.", "criteria": criteria}},
            "options": {"head": "general-v1", **options}}


def _reply(logprobs, model=MODEL, dim=DIM):
    choice = {"logprobs": {"content": [{"top_logprobs": [{"token": t, "logprob": v} for t, v in logprobs.items()]}]}}
    if dim:
        choice["strata_hidden"] = {"dtype": "float32", "dim": dim,
                                   "b64": base64.b64encode(np.ones(dim, "<f4").tobytes()).decode()}
    return {"model": model, "choices": [choice], "usage": {"prompt_tokens": 10}}


def _run(req, heads, reply):
    calls = []

    async def invoke(body):
        calls.append(body)
        return reply
    return asyncio.run(classify(req, invoke, heads)), calls


def test_the_head_moves_the_decision_and_asks_for_the_hidden_state(heads):
    out, calls = _run(_req(), heads, _reply({"A": -0.1, "B": -3.0, "C": -4.0}))
    assert calls[0]["strata_hidden"] is True
    assert out["head"] == "general-v1"
    assert out["answers"]["q"]["choice"] == "y"                 # -3 + 5 beats -0.1


def test_a_label_past_the_top_logprobs_is_floored_not_refused(heads):
    out, _ = _run(_req(), heads, _reply({"A": -0.1, "C": -4.0}))   # B missing: scored at -4.0, then +5
    assert out["answers"]["q"]["choice"] == "y"


@pytest.mark.parametrize("reply, match", [
    (_reply({"A": -0.1, "B": -3.0, "C": -4.0}, model="qwen3.8-27b"), "trained on"),
    (_reply({"A": -0.1, "B": -3.0, "C": -4.0}, dim=0), "no hidden state"),
    (_reply({"A": -0.1, "B": -3.0, "C": -4.0}, dim=8), "expects 4"),
])
def test_refused_when_the_reply_does_not_fit_the_head(heads, reply, match):
    with pytest.raises(SimpleJevError, match=match):
        _run(_req(), heads, reply)


@pytest.mark.parametrize("req, registered, match", [
    (_req(head="other"), True, "not registered"),
    (_req(), False, "not registered"),
    (_req(qtype="score"), True, "choice questions only"),
])
def test_refused_before_the_model_is_asked(heads, req, registered, match):
    async def invoke(body):
        raise AssertionError("must refuse before calling the model")
    with pytest.raises(SimpleJevError, match=match):
        asyncio.run(classify(req, invoke, heads if registered else None))


def test_no_head_no_hidden_state_requested(heads):
    req = _req()
    del req["options"]
    out, calls = _run(req, heads, _reply({"A": -0.1, "B": -3.0, "C": -4.0}))
    assert "strata_hidden" not in calls[0] and "head" not in out
    assert out["answers"]["q"]["choice"] == "x"
