"""Map upstream Laya/laya-mlx answers onto the frozen public contract.

Upstream binary answers use the field ``noul``. The public v1 contract
forbids that alias and requires ``probability``. Extra keys (confidence,
action) are dropped rather than forwarded.
"""
from __future__ import annotations

from typing import Any, Dict, Mapping


def map_upstream_answer(ans: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(ans, dict):
        raise ValueError("upstream answer must be an object")
    typ = ans.get("type")
    if typ == "noul":
        p = ans.get("probability", ans.get("noul"))
        if p is None:
            raise ValueError("noul answer missing probability")
        return {"type": "noul", "probability": float(p)}
    if typ == "choice":
        probs = ans.get("probabilities") or {}
        choice = ans.get("choice")
        return {
            "type": "choice",
            "choice": str(choice),
            "probabilities": {str(k): float(v) for k, v in probs.items()},
        }
    raise ValueError(f"unsupported upstream type {typ}")
