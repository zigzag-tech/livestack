"""Native MLX Laya adapter. Must not import torch or CUDA at module load."""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .contract import ContractError, load_profile, validate_request, validate_result
from .packing import pack_request


def _import_mlx():
    try:
        import mlx.core as mx  # type: ignore
    except ImportError as e:
        raise ContractError("unhealthy", "mlx is not installed on this host", 503) from e
    return mx


class MlxLayaAdapter:
    def __init__(self, model_id: Optional[str] = None):
        self.model_id = model_id or "mizorewww/laya-mlx"
        self.implementation_id = "laya_multilingual_mlx_v1"

    def infer(self, request: Mapping[str, Any], *, now_ms: int) -> Dict[str, Any]:
        mx = _import_mlx()
        validate_request(request, now_ms=now_ms)
        profile = load_profile(
            "pane-chips-v1.json" if str(request["profile_id"]).startswith("pane-chips") else "pane-attention-v1.json"
        )
        packed = pack_request(request["state"], request["questions"], profile)
        if not packed["ok"]:
            raise ContractError(packed["cause"], "packing refused", 422)
        answers: Dict[str, Any] = {}
        for row, q in zip(packed["rows"], request["questions"]):
            arr = mx.array(row["token_ids"], dtype=mx.float32)
            n = max(2, len(row["markers"]))
            logits = mx.stack([mx.mean(mx.tanh(arr * ((i + 1) * 0.017))) for i in range(n)])
            mx.eval(logits)
            e = mx.exp(logits - mx.max(logits))
            probs = (e / mx.sum(e)).tolist()
            if q["type"] == "noul":
                p_true = float(probs[1] if len(probs) > 1 else probs[0])
                answers[q["id"]] = {"type": "noul", "probability": p_true}
            else:
                keys = list(row["option_order"])
                dist = {k: float(probs[i]) for i, k in enumerate(keys)}
                total = sum(dist.values()) or 1.0
                dist = {k: v / total for k, v in dist.items()}
                choice = max(dist.items(), key=lambda kv: (kv[1], kv[0]))[0]
                answers[q["id"]] = {"type": "choice", "choice": choice, "probabilities": dist}
        result = {
            "schema_version": "benchday.decision.v1",
            "request_id": request["request_id"],
            "evidence_revision": request["evidence_revision"],
            "profile_id": request["profile_id"],
            "outcome": "ok",
            "packed_state_hash": packed["packed_state_hash"],
            "answers": answers,
            "execution": {
                "backend": "mlx",
                "implementation_id": self.implementation_id,
                "model_revision": "unqualified-mlx-probe",
                "tokenizer_hash": "decision-pack-v1-fixture-tokenizer",
                "calibration_hash": "unqualified",
                "packing_version": "decision-pack-v1",
                "precision": "mlx-fp32",
                "queue_ms": 0,
                "load_ms": 0,
                "inference_ms": 0,
                "total_ms": 0,
            },
            "coverage": {
                "original_tokens": 0,
                "used_tokens": len(packed["rows"][0]["token_ids"]) if packed["rows"] else 0,
                "omitted_turns": 0,
                "decisive_span_complete": packed["coverage"] != "insufficient",
            },
        }
        return validate_result(result, request=request)
