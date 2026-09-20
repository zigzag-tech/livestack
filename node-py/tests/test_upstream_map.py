"""Drive the shipped Laya→contract mapper. No reimplementation."""
from livestack_node.decisions.upstream_map import map_upstream_answer


def test_noul_alias_becomes_probability_and_drops_extras():
    out = map_upstream_answer({"type": "noul", "noul": 0.0074, "confidence": 0.99, "action": {"act_probability": 1}})
    assert out == {"type": "noul", "probability": 0.0074}
    assert "noul" not in out
    assert "confidence" not in out


def test_choice_keeps_argmax_distribution():
    out = map_upstream_answer({
        "type": "choice",
        "choice": "question",
        "probabilities": {"question": 0.7924, "working": 0.1, "unknown": 0.1076},
        "confidence": 0.2,
    })
    assert out["type"] == "choice"
    assert out["choice"] == "question"
    assert out["probabilities"]["question"] == 0.7924
    assert "confidence" not in out
