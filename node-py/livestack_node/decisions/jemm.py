"""JEMM: a decision LoRA's own prompt and calibrated scoring, as a Simple Jev template.

JEMM (huggingface.co/MaestroYan/JEMM, Apache-2.0) is a rank-16 LoRA for
Qwen3.8-27B trained to make the one-label decision Simple Jev makes by prompting
the base model. Harmony serves it as the adapter `jemm` beside the base (see
HARMONY.md, "Unit composition"). This module compiles a choice question into
JEMM's prompt, as its model card specifies it, and turns the permitted label
logprobs into probabilities with a temperature fitted on OUR served model: the
card's temperature was fitted on the bf16 base, and ours is int4.

Choice questions only. The card documents only the choice prompt, and a
`score`/`noul` question refuses by name rather than being coerced into one.
Selected per request with `options: {"template": "jemm"}`; the default Simple
Jev path is untouched.
"""
from __future__ import annotations

import json
import math
import os
from typing import Any, Mapping, Tuple

LABELS = [chr(65 + i) for i in range(26)] + list("012345")
SYSTEM = ("Choose the best available candidate for the question using only the supplied state. "
          "Return exactly one candidate label.")
TEMPLATE_VERSION = "jemm-v1"
CALIBRATION_PATH = os.path.join(os.path.dirname(__file__), "jemm_calibration.json")


def calibration(path: str = CALIBRATION_PATH) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _flat(s: str) -> str:
    return " ".join(str(s).split())


def render_state(state: Any) -> str:
    """A mapping renders as `key:` blocks (multi-line text stays readable to the
    model); anything else as compact JSON. The calibration was fitted on this
    exact rendering, so changing it means refitting."""
    if isinstance(state, Mapping):
        parts = []
        for k, v in state.items():
            parts.append(f"{k}:\n{v}" if isinstance(v, str) else
                         f"{k}: {json.dumps(v, ensure_ascii=False, separators=(',', ':'))}")
        return "\n".join(parts)
    return json.dumps(state, ensure_ascii=False, separators=(",", ":"))


def adapter_model(model: str, adapter: str) -> str:
    """Route to the adapter the way Harmony selects one: a requirement gains
    `adapter=<name>`; a caller with no opinion names the adapter."""
    model = (model or "").strip()
    if model.startswith("require:"):
        return f"{model},adapter={adapter}"
    return adapter


def compile_question(state: Any, question: Mapping[str, Any], *, max_options: int) -> Tuple[list, dict]:
    from .simple_jev import SimpleJevError
    if question["type"] != "choice":
        raise SimpleJevError(f"template {TEMPLATE_VERSION} answers choice questions only, "
                             f"not {question['type']!r}")
    criteria = question["criteria"]
    answers = list(criteria)
    if len(answers) > max_options:
        raise SimpleJevError(f"{len(answers)} candidates exceed the {max_options} this endpoint "
                             f"can score (vLLM top_logprobs)")
    labels = LABELS[:len(answers)]
    options = "\n".join(f"{lab}) {_flat(f'{a}: {criteria[a]}')}" for lab, a in zip(labels, answers))
    text = (f"State:\n{render_state(state).strip()}\n\nQuestion: {_flat(question['instructions'])}\n\n"
            f"Candidates:\n{options}\n\nAnswer with exactly one candidate label.")
    messages = [{"role": "system", "content": SYSTEM}, {"role": "user", "content": text}]
    return messages, {"labels": labels, "answers": answers}


def request_body(model: str, messages: list, labels: list, adapter: str, top_logprobs: int) -> dict:
    return {"model": adapter_model(model, adapter), "messages": messages,
            "chat_template_kwargs": {"enable_thinking": False},
            "temperature": 0, "max_tokens": 1, "logprobs": True, "top_logprobs": top_logprobs,
            "structured_outputs": {"choice": labels}}


def score(plan: Mapping[str, Any], alternatives: list, cal: Mapping[str, Any]) -> dict:
    from .simple_jev import SimpleJevError
    logits = {a.get("token"): float(a["logprob"]) for a in alternatives
              if isinstance(a, Mapping) and isinstance(a.get("token"), str) and "logprob" in a}
    missing = [l for l in plan["labels"] if l not in logits]
    if missing:
        raise SimpleJevError(f"permitted labels absent from model logprobs: {missing}")
    t = float(cal["temperature"])
    z = [logits[l] / t for l in plan["labels"]]
    pivot = max(z)
    w = [math.exp(v - pivot) for v in z]
    total = sum(w)
    probs = [v / total for v in w]
    best = max(range(len(probs)), key=probs.__getitem__)
    return {"type": "choice", "choice": plan["answers"][best], "confidence": probs[best],
            "probabilities": dict(zip(plan["answers"], probs)),
            # The card's rule: a top probability under `threshold` is undecided.
            # Reported, never acted on here; the caller decides what undecided means.
            "undecided": probs[best] < float(cal["threshold"])}
