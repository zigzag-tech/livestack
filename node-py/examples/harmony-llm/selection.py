"""Unit selection under a caller's `prefer` (harmony-engine-units design §4b.3).

`prefer` orders units that already satisfied the request's HARD requirement.
It never swaps anything on its own (a resident unit keeps answering) and it
never invents a value: a metric nobody measured is ABSENT from the inventory,
and `preferences.observation()` reads absence as "no opinion" — silence is not
a zero.

What one unit publishes here is what its launch line says (`params_b`, served
`context_len`) plus what its engine MEASURED (`decode_tok_s`,
`first_token_ms`) — carried from harmony-llm's fitted cost rows with their
sample counts, exactly like the `asr.*` metrics. The `model_revision` is what
makes the numbers comparable across units: the composition hash a measurement
was taken for, or the declared model when nothing has been measured.
"""
from __future__ import annotations

import time
from typing import Mapping, Optional

# How long a published number stays an opinion. Measured rows are re-taken on
# every restart; a day is the outside bound before a silent number stops
# ordering anything (`observation()` refuses stale).
_TTL_S = 86400.0


def unit_inventory(attrs: Mapping, fitted: "Optional[Mapping]" = None,
                   revision: str = "") -> dict:
    """One unit's `prefer` inventory: {"llm": {<metric>: observation}}.

    Empty dict when the unit publishes nothing — which is an answer ("no
    opinion"), never a zero."""
    now = time.time()
    llm: dict = {}
    for key in ("params_b", "context_len"):
        v = attrs.get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            llm[key] = {"value": float(v), "sample_count": 1,
                        "measured_at": now, "ttl_s": _TTL_S,
                        "model_revision": revision or "declared"}
    for key in ("decode_tok_s", "first_token_ms"):
        v = (fitted or {}).get(key)
        if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
            llm[key] = {
                "value": float(v),
                "sample_count": int((fitted or {}).get(f"{key}_samples") or 1),
                "measured_at": float((fitted or {}).get("measured_at") or now),
                "ttl_s": _TTL_S,
                "model_revision": revision or "measured"}
    return {"llm": llm} if llm else {}
