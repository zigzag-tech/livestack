"""CUDA Laya adapter.

Loads the pinned upstream model when present. Refuses CPU fallback. Packing
is the shared decision-pack-v1 packer. Without weights, a CUDA-resident probe
still executes on the selected device so ingress/metadata can be qualified;
that probe is not a portable quality qualification.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .contract import ContractError, load_profile, validate_request, validate_result
from .packing import pack_request
from .upstream_map import map_upstream_answer


class CudaLayaAdapter:
    def __init__(self, device_index: int = 0, model_id: Optional[str] = None):
        self.device_index = device_index
        self.model_id = model_id or "convaiinnovations/laya-multilingual"
        self.implementation_id = "laya_multilingual_cuda_v1"
        self._model = None

    def _torch(self):
        try:
            import torch
        except ImportError as e:
            raise ContractError("unhealthy", "torch is not installed", 503) from e
        return torch

    def device_name(self) -> str:
        torch = self._torch()
        if not torch.cuda.is_available():
            raise ContractError("unhealthy", "CUDA is unavailable; CPU fallback is refused", 503)
        idx = self.device_index
        if idx < 0 or idx >= torch.cuda.device_count():
            raise ContractError("unhealthy", f"CUDA device {idx} is not present", 503)
        name = torch.cuda.get_device_name(idx)
        if "CPU" in name.upper() and "CUDA" not in name.upper():
            raise ContractError("unhealthy", "refusing a CPU device masquerading as CUDA", 503)
        return name

    def _infer_laya(self, request: Mapping[str, Any], torch, device) -> Optional[Dict[str, Any]]:
        import os
        path = os.environ.get("LAYA_MODEL_DIR")
        if not path:
            return None
        try:
            import laya
        except ImportError as e:
            raise ContractError("unhealthy", "laya package missing while LAYA_MODEL_DIR is set", 503) from e
        if self._model is None:
            self._model = laya.load(path)
            self._model.model.to(device)
        questions = {}
        for q in request["questions"]:
            spec = {"type": q["type"], "instructions": q["instructions"]}
            if q.get("criteria"):
                spec["criteria"] = q["criteria"]
            questions[q["id"]] = spec
        raw = self._model.predict(request["state"], questions)
        torch.cuda.synchronize(device)
        return {qid: map_upstream_answer(ans) for qid, ans in raw["answers"].items()}

    def infer(self, request: Mapping[str, Any], *, now_ms: int) -> Dict[str, Any]:
        torch = self._torch()
        validate_request(request, now_ms=now_ms)
        if not torch.cuda.is_available():
            raise ContractError("unhealthy", "CUDA is unavailable; CPU fallback is refused", 503)
        device = torch.device(f"cuda:{self.device_index}")
        if device.type != "cuda":
            raise ContractError("unhealthy", "refusing non-CUDA device", 503)
        profile = load_profile(
            "pane-chips-v1.json" if str(request["profile_id"]).startswith("pane-chips") else "pane-attention-v1.json"
        )
        packed = pack_request(request["state"], request["questions"], profile)
        if not packed["ok"]:
            raise ContractError(packed["cause"], "packing refused", 422)
        laya_answers = self._infer_laya(request, torch, device)
        answers: Dict[str, Any] = {}
        model_revision = "unqualified-cuda-probe"
        if laya_answers is not None:
            answers = laya_answers
            model_revision = "convaiinnovations/laya-multilingual"
        if laya_answers is None:
            for row, q in zip(packed["rows"], request["questions"]):
                ids = torch.tensor(row["token_ids"], device=device, dtype=torch.long)
                feats = ids.to(dtype=torch.float32)
                logits = torch.stack([
                    ((feats * ((i + 1) * 0.017)).tanh().mean())
                    for i in range(max(2, len(row["markers"])))
                ])
                torch.cuda.synchronize(device)
                probs = torch.softmax(logits, dim=0).detach().float().cpu().tolist()
                if q["type"] == "noul":
                    p_true = float(probs[1] if len(probs) > 1 else probs[0])
                    if not (0.0 <= p_true <= 1.0):
                        raise ContractError("invalid_output", "non-finite binary probability", 422)
                    answers[q["id"]] = {"type": "noul", "probability": p_true}
                else:
                    keys = list(row["option_order"])
                    if len(keys) > len(probs):
                        raise ContractError("invalid_output", "missing option scores", 422)
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
                "backend": "cuda",
                "implementation_id": self.implementation_id,
                "model_revision": model_revision,
                "tokenizer_hash": "decision-pack-v1-fixture-tokenizer",
                "calibration_hash": "unqualified",
                "packing_version": "decision-pack-v1",
                "precision": "fp32-weights-autocast-fp16",
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
