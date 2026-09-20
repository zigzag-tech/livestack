"""Deterministic decision backend for contract/auth/admission tests.

It implements the public typed-decision contract and controlled faults. It
does not imitate model reasoning. H08/H09 require the real CUDA/MLX adapters.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Mapping, Optional

from .contract import ContractError, load_profile, validate_request, validate_result
from .packing import pack_request


ATTENTION_ORDER = ["question", "working", "self_waiting", "finished_turn", "idle", "unknown"]


class FixtureBackend:
    def __init__(self, backend: str, implementation_id: str):
        if backend not in ("cuda", "mlx"):
            raise ValueError("fixture backend must name a physical cuda or mlx kind")
        self.backend = backend
        self.implementation_id = implementation_id
        self.calls = 0
        self.fail_next: Optional[str] = None
        self.delay_until_ms = 0
        self.unhealthy = False

    def infer(self, request: Mapping[str, Any], *, now_ms: int) -> Dict[str, Any]:
        if self.unhealthy:
            raise ContractError("unhealthy", "fixture backend marked unhealthy", 503)
        if self.fail_next:
            cause = self.fail_next
            self.fail_next = None
            raise ContractError(cause, f"injected {cause}", 422 if cause == "invalid_output" else 503)
        validate_request(request, now_ms=now_ms)
        if now_ms >= int(request["deadline_at_ms"]):
            raise ContractError("deadline_exceeded", "deadline expired before inference", 504)
        profile = _profile_for(request["profile_id"])
        packed = pack_request(request["state"], request["questions"], profile)
        if not packed["ok"]:
            raise ContractError(packed["cause"], "packing refused", 422)
        answers: Dict[str, Any] = {}
        for q in request["questions"]:
            if q["type"] == "choice":
                answers[q["id"]] = _choice_from_state(q, request["state"])
            else:
                answers[q["id"]] = {"type": "noul", "probability": _noul_from_id(q["id"])}
        result = {
            "schema_version": "benchday.decision.v1",
            "request_id": request["request_id"],
            "evidence_revision": request["evidence_revision"],
            "profile_id": request["profile_id"],
            "outcome": "ok",
            "packed_state_hash": packed["packed_state_hash"],
            "answers": answers,
            "execution": {
                "backend": self.backend,
                "implementation_id": self.implementation_id,
                "model_revision": "fixture",
                "tokenizer_hash": "sha256:" + ("ab" * 32),
                "calibration_hash": "unqualified",
                "packing_version": "decision-pack-v1",
                "precision": "fixture",
                "queue_ms": 0,
                "load_ms": 0,
                "inference_ms": 1,
                "total_ms": 1,
            },
            "coverage": {
                "original_tokens": 0,
                "used_tokens": 0,
                "omitted_turns": 0,
                "decisive_span_complete": packed["coverage"] != "insufficient",
            },
        }
        self.calls += 1
        return validate_result(result, request=request)


def _profile_for(profile_id: str) -> Dict[str, Any]:
    if profile_id.startswith("pane-chips"):
        return load_profile("pane-chips-v1.json")
    return load_profile("pane-attention-v1.json")


def _choice_from_state(question: Mapping[str, Any], state: Mapping[str, Any]) -> Dict[str, Any]:
    text = (state.get("current_agent_message") or "").lower()
    if "?" in text or "？" in text or "which" in text or "哪个" in text:
        label = "question"
    elif "nothing needs you" in text:
        label = "finished_turn"
    else:
        label = "working"
    keys = list(question.get("criteria") or ATTENTION_ORDER)
    n = len(keys)
    winner = 0.90
    rest = (1.0 - winner) / max(1, n - 1)
    probs = {k: (winner if k == label else rest) for k in keys}
    # keep unrounded; tiny fix so sum is exact
    total = sum(probs.values())
    if keys:
        probs[keys[0]] += 1.0 - total
    return {"type": "choice", "choice": label, "probabilities": probs}


def _noul_from_id(qid: str) -> float:
    # Stable, finite, in (0,1), not a click-rate alias.
    acc = 0
    for ch in qid:
        acc = (acc * 33 + ord(ch)) & 0xFFFFFFFF
    return round(0.05 + (acc % 9000) / 10000.0, 6)
