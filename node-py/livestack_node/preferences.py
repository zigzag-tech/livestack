"""Bounded, typed fleet-ranking preferences.

Preferences order candidates that already passed hard eligibility.  They never
invent a value for a silent node and never turn a soft objective into refusal.
"""
from __future__ import annotations

import json
import math
import time
from typing import Any, Iterable


class PreferenceError(ValueError):
    pass


ATTRIBUTES = {"asr.streaming", "asr.languages"}
METRICS = {
    "asr.quality": "max",
    "asr.first_partial_ms": "min",
    "asr.finalization_ms": "min",
    "asr.failure_rate": "min",
}
MAX_PREFERENCES = 8
MAX_WIRE_BYTES = 4096


def parse_preferences(raw: str | None) -> list[dict]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, str) or len(raw.encode()) > MAX_WIRE_BYTES:
        raise PreferenceError("prefer must be JSON no larger than 4096 bytes")
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise PreferenceError("prefer must be a JSON array") from exc
    if not isinstance(value, list) or len(value) > MAX_PREFERENCES:
        raise PreferenceError("prefer must contain at most 8 clauses")
    out = []
    for i, clause in enumerate(value):
        if not isinstance(clause, dict):
            raise PreferenceError(f"prefer[{i}] must be an object")
        if "attribute" in clause:
            if set(clause) != {"attribute", "value"} or clause["attribute"] not in ATTRIBUTES:
                raise PreferenceError(f"prefer[{i}] has an unknown attribute or fields")
            val = clause["value"]
            if not isinstance(val, (str, bool)) and not (
                    isinstance(val, list) and val and len(val) <= 32
                    and all(isinstance(v, str) and len(v) <= 128 for v in val)):
                raise PreferenceError(f"prefer[{i}] has an invalid value")
            out.append(dict(clause))
            continue
        if "metric" in clause:
            allowed = {"metric", "direction", "evaluation", "evaluation_revision",
                       "cohort", "min_samples"}
            metric = clause.get("metric")
            direction = clause.get("direction")
            if set(clause) - allowed or metric not in METRICS or direction not in ("min", "max"):
                raise PreferenceError(f"prefer[{i}] has an unknown metric or fields")
            if direction != METRICS[metric]:
                raise PreferenceError(f"prefer[{i}] direction contradicts metric semantics")
            minimum = clause.get("min_samples", 1)
            if isinstance(minimum, bool) or not isinstance(minimum, int) or not 1 <= minimum <= 1_000_000:
                raise PreferenceError(f"prefer[{i}] min_samples is invalid")
            for key in ("evaluation", "evaluation_revision", "cohort"):
                if key in clause and (not isinstance(clause[key], str) or not 1 <= len(clause[key]) <= 160):
                    raise PreferenceError(f"prefer[{i}] {key} is invalid")
            if metric == "asr.quality" and not all(clause.get(k) for k in (
                    "evaluation", "evaluation_revision", "cohort")):
                raise PreferenceError("asr.quality requires evaluation, evaluation_revision, and cohort")
            out.append({**clause, "min_samples": minimum})
            continue
        raise PreferenceError(f"prefer[{i}] must name attribute or metric")
    return out


def _lookup(mapping: Any, dotted: str):
    cur = mapping
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def observation(inventory: dict | None, clause: dict, now: float) -> tuple[float | None, str]:
    raw = _lookup(inventory or {}, clause["metric"])
    if not isinstance(raw, dict):
        return None, "no observation"
    value = raw.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None, "invalid observation"
    if raw.get("sample_count", 0) < clause.get("min_samples", 1):
        return None, "undersampled"
    measured = raw.get("measured_at")
    ttl = raw.get("ttl_s")
    if (not isinstance(measured, (int, float)) or not isinstance(ttl, (int, float))
            or ttl <= 0 or now - measured > ttl):
        return None, "stale or unbounded"
    for key in ("evaluation", "evaluation_revision", "cohort"):
        wanted = clause.get(key)
        if wanted is not None and raw.get(key) != wanted:
            return None, f"incomparable {key}"
    if not isinstance(raw.get("model_revision"), str) or not raw["model_revision"]:
        return None, "model revision absent"
    return float(value), "comparable"


def preference_key(inventory: dict | None, clauses: Iterable[dict], now: float | None = None):
    now = time.time() if now is None else now
    key, receipt = [], []
    for clause in clauses:
        if "attribute" in clause:
            actual = _lookup(inventory or {}, clause["attribute"])
            wanted = clause["value"]
            match = wanted in actual if isinstance(actual, list) else actual == wanted
            key.append(0 if match else 1)
            receipt.append({"clause": clause, "value": actual,
                            "comparable": actual is not None, "matched": match})
        else:
            value, reason = observation(inventory, clause, now)
            # Known evidence always outranks silence; direction controls value.
            key.extend((0 if value is not None else 1,
                        (-value if clause["direction"] == "max" else value)
                        if value is not None else 0.0))
            receipt.append({"clause": clause, "value": value,
                            "comparable": value is not None, "reason": reason})
    return tuple(key), receipt
