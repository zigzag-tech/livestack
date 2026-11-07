from __future__ import annotations

import asyncio

import pytest

from livestack_node.decisions.simple_jev import SimpleJevError, classify, compile_branch, score_branch


def _request():
    return {
        "model": "local",
        "state": "Mia owns a red bicycle.",
        "questions": {"color": {"type": "choice", "instructions": "What color is the bicycle?",
                                  "criteria": {"red": None, "blue": None}}},
    }


def test_compile_uses_unfinished_assistant_prefill():
    messages, plan = compile_branch(_request(), "color")
    assert messages[-1] == {"role": "assistant", "content": '{"answer": "'}
    assert plan["labels"] == ["A", "B"]
    assert '"answer":"red"' in messages[-2]["content"]


def test_score_normalizes_only_permitted_labels():
    _, plan = compile_branch(_request(), "color")
    answer = score_branch(plan, [{"token": "A", "logprob": -0.1},
                                 {"token": "B", "logprob": -2.1},
                                 {"token": "noise", "logprob": 0.0}])
    assert answer["choice"] == "red"
    assert answer["probabilities"]["red"] > 0.8
    assert sum(answer["probabilities"].values()) == pytest.approx(1.0)


def test_missing_label_fails_closed():
    _, plan = compile_branch(_request(), "color")
    with pytest.raises(SimpleJevError, match="absent"):
        score_branch(plan, [{"token": "A", "logprob": -0.1}])


def test_score_keeps_the_public_rubric_in_its_legend():
    request = {"state": "critical", "questions": {"urgency": {
        "type": "score", "instructions": "How urgent?", "criteria": ["Routine", "Critical"]}}}
    _, plan = compile_branch(request, "urgency")
    answer = score_branch(plan, [{"token": "0", "logprob": -2.0}, {"token": "1", "logprob": -0.1}])
    assert answer["legend"] == {"0": "Routine", "1": "Critical"}
    assert answer["score"] > 0.8


def test_classifier_returns_served_model_and_answers():
    calls = []

    async def invoke(body):
        calls.append(body)
        return {"model": "real/revision", "choices": [{"logprobs": {"content": [{"top_logprobs": [
            {"token": "A", "logprob": -0.1}, {"token": "B", "logprob": -2.1}
        ]}]}}], "usage": {"prompt_tokens": 123}}

    result = asyncio.run(classify(_request(), invoke))
    assert result["model"] == "real/revision"
    assert result["template_version"] == "v1"
    assert result["answers"]["color"]["choice"] == "red"
    assert calls[0]["continue_final_message"] is True
    assert calls[0]["add_generation_prompt"] is False
    assert calls[0]["max_tokens"] == 1
    assert calls[0]["structured_outputs"] == {"choice": ["A", "B"]}


def test_image_evidence_is_compiled_as_pictures_after_the_state_text():
    import base64, hashlib
    from livestack_node.decisions.simple_jev import compile_branch, SimpleJevError, validate_request
    data = b"\x89PNG\r\n\x1a\nfake"
    payload = {"model": "m", "state": {"evidence": [{"id": "note", "text": "a frieze"}, {"id": "view", "image": {"media_type": "image/png", "b64_json": base64.b64encode(data).decode(), "sha256": hashlib.sha256(data).hexdigest()}}]},
               "questions": {"q": {"type": "choice", "instructions": "Is there a cut?", "criteria": {"no_cut": "no", "cut": "yes"}}}}
    messages, _ = compile_branch(payload, "q")
    user = messages[1]["content"]
    assert isinstance(user, list)
    assert "picture view below" in user[0]["text"] and base64.b64encode(data).decode() not in user[0]["text"]
    assert user[2] == {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(data).decode()}}
    assert "Is there a cut?" in user[-1]["text"]
    bad = {**payload, "state": {"evidence": [{"id": "view", "image": {**payload["state"]["evidence"][1]["image"], "sha256": "0" * 64}}]}}
    with pytest.raises(SimpleJevError):
        validate_request(bad)
