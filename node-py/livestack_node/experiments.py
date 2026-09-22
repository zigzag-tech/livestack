"""Bounded, deterministic capability experiments for fleet routing."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import sqlite3
import time


class ExperimentError(ValueError):
    pass


MAX_CONFIG_BYTES = 64 * 1024
MAX_EXPERIMENTS = 32
MAX_VARIANTS = 16


def validate_policy(value: dict) -> dict:
    allowed = {"id", "kind", "cohorts", "control", "variants", "exploration_share",
               "control_floor", "min_samples", "confidence", "guardrails",
               "hysteresis_margin", "retention_days", "max_observations", "promoted", "paused"}
    if not isinstance(value, dict) or set(value) - allowed:
        raise ExperimentError("invalid experiment fields")
    for key in ("id", "kind", "control"):
        if not isinstance(value.get(key), str) or not value[key]:
            raise ExperimentError(f"experiment {key} is required")
    variants = value.get("variants")
    if (not isinstance(variants, list) or not 2 <= len(variants) <= MAX_VARIANTS
            or len(set(variants)) != len(variants) or value["control"] not in variants
            or not all(isinstance(x, str) and 1 <= len(x) <= 160 for x in variants)):
        raise ExperimentError("experiment variants are invalid")
    cohorts = value.get("cohorts")
    if not isinstance(cohorts, list) or not 1 <= len(cohorts) <= 64 or not all(
            isinstance(x, str) and x for x in cohorts):
        raise ExperimentError("experiment cohorts are invalid")
    share, floor = value.get("exploration_share"), value.get("control_floor")
    if any(isinstance(x, bool) or not isinstance(x, (int, float)) or not 0 <= x <= 1
           for x in (share, floor)) or share > .5 or floor < .05:
        raise ExperimentError("exploration_share must be <= .5 and control_floor >= .05")
    samples = value.get("min_samples")
    if isinstance(samples, bool) or not isinstance(samples, int) or not 10 <= samples <= 1_000_000:
        raise ExperimentError("min_samples must be in [10, 1000000]")
    confidence = value.get("confidence")
    if not isinstance(confidence, (int, float)) or not .5 <= confidence < 1:
        raise ExperimentError("confidence must be in [.5, 1)")
    margin = value.get("hysteresis_margin")
    if not isinstance(margin, (int, float)) or not 0 <= margin <= .25:
        raise ExperimentError("hysteresis_margin must be in [0, .25]")
    guards = value.get("guardrails")
    if not isinstance(guards, dict) or not guards or set(guards) - {
            "max_failure_rate", "max_finalization_ms"}:
        raise ExperimentError("guardrails must be bounded and recognized")
    for number in guards.values():
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number) or number < 0:
            raise ExperimentError("guardrail values must be finite and nonnegative")
    days, cap = value.get("retention_days"), value.get("max_observations")
    if not isinstance(days, int) or not 1 <= days <= 365 or not isinstance(cap, int) or not 100 <= cap <= 1_000_000:
        raise ExperimentError("experiment retention bounds are invalid")
    if value.get("promoted") is not None and value["promoted"] not in variants:
        raise ExperimentError("promoted variant is unknown")
    return dict(value)


def load_policies(path: str) -> dict[str, dict]:
    with open(path, "rb") as handle:
        raw = handle.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise ExperimentError("experiment config exceeds 64 KiB")
    try:
        values = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentError("experiment config is invalid JSON") from exc
    if not isinstance(values, list) or len(values) > MAX_EXPERIMENTS:
        raise ExperimentError("experiment config must contain at most 32 policies")
    result = {}
    for value in values:
        policy = validate_policy(value)
        if policy["id"] in result:
            raise ExperimentError("duplicate experiment id")
        result[policy["id"]] = policy
    return result


def assignment(policy: dict, subject: str, cohort: str, available: set[str]) -> dict:
    if not subject or cohort not in policy["cohorts"] or policy.get("paused"):
        return {"variant": policy["control"], "mode": "control", "reason": "experiment inactive"}
    eligible = [v for v in policy["variants"] if v in available]
    control = policy["control"]
    if control not in eligible:
        return {"variant": None, "mode": "unassigned", "reason": "control unavailable"}
    promoted = policy.get("promoted")
    digest = hashlib.sha256(f"{policy['id']}\0{cohort}\0{subject}".encode()).digest()
    bucket = int.from_bytes(digest[:8], "big") / 2**64
    explore = [v for v in eligible if v != control]
    if explore and bucket < policy["exploration_share"]:
        pick = explore[int.from_bytes(digest[8:12], "big") % len(explore)]
        return {"variant": pick, "mode": "exploration", "reason": "stable exposure bucket"}
    if promoted in eligible and bucket >= policy["control_floor"]:
        return {"variant": promoted, "mode": "promoted", "reason": "qualified promotion"}
    return {"variant": control, "mode": "control", "reason": "control floor or default"}


class ExperimentStore:
    """Content-free observations with active age and count enforcement."""
    def __init__(self, path: str, policy: dict):
        self.policy = validate_policy(policy)
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS observations("
                        "id INTEGER PRIMARY KEY, at REAL NOT NULL, subject TEXT NOT NULL, "
                        "cohort TEXT NOT NULL, variant TEXT NOT NULL, metric TEXT NOT NULL, "
                        "value REAL NOT NULL, source TEXT NOT NULL)")
        self.db.execute("CREATE UNIQUE INDEX IF NOT EXISTS obs_once ON observations("
                        "subject, metric)")
        self.prune()

    def observe(self, *, subject: str, cohort: str, variant: str, metric: str,
                value: float, source: str, at: float | None = None):
        if cohort not in self.policy["cohorts"] or variant not in self.policy["variants"]:
            raise ExperimentError("observation is outside experiment policy")
        if metric not in ("failure", "first_partial_ms", "finalization_ms", "accuracy"):
            raise ExperimentError("unknown observation metric")
        if metric == "accuracy" and source not in ("ground_truth", "correction", "versioned_evaluation"):
            raise ExperimentError("accuracy requires attributable ground truth")
        if source not in ("runtime", "ground_truth", "correction", "versioned_evaluation"):
            raise ExperimentError("unknown observation source")
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ExperimentError("observation value must be finite")
        self.db.execute("INSERT OR IGNORE INTO observations(at,subject,cohort,variant,metric,value,source) "
                        "VALUES(?,?,?,?,?,?,?)", (at or time.time(), subject, cohort, variant,
                                                  metric, float(value), source))
        self.db.commit()
        self.prune(at)

    def prune(self, now: float | None = None):
        now = time.time() if now is None else now
        self.db.execute("DELETE FROM observations WHERE at < ?",
                        (now - self.policy["retention_days"] * 86400,))
        cap = self.policy["max_observations"]
        self.db.execute("DELETE FROM observations WHERE id IN (SELECT id FROM observations "
                        "ORDER BY at DESC, id DESC LIMIT -1 OFFSET ?)", (cap,))
        self.db.commit()

    def summary(self, cohort: str) -> dict:
        rows = self.db.execute("SELECT variant,metric,COUNT(*),AVG(value) FROM observations "
                               "WHERE cohort=? GROUP BY variant,metric", (cohort,)).fetchall()
        result = {}
        for variant, metric, count, mean in rows:
            result.setdefault(variant, {})[metric] = {"count": count, "mean": mean}
        return result


def decision(policy: dict, summary: dict, current: str | None = None) -> dict:
    """Conservative automatic state: guards first, then evidence-backed quality."""
    minimum, control = policy["min_samples"], policy["control"]
    eligible, guarded = [], set()
    for variant in policy["variants"]:
        stats = summary.get(variant, {})
        failure = stats.get("failure")
        final = stats.get("finalization_ms")
        if failure and failure["count"] >= minimum and failure["mean"] > policy["guardrails"].get("max_failure_rate", 1):
            guarded.add(variant)
            continue
        if final and final["count"] >= minimum and final["mean"] > policy["guardrails"].get("max_finalization_ms", float("inf")):
            guarded.add(variant)
            continue
        quality = stats.get("accuracy")
        if quality and quality["count"] >= minimum:
            eligible.append((quality["mean"], variant))
    if not eligible:
        return {"promoted": None, "reason": "insufficient comparable accuracy evidence"}
    eligible.sort(reverse=True)
    winner = eligible[0][1]
    scores = dict((variant, score) for score, variant in eligible)
    if current in scores and current not in guarded:
        if scores[winner] < scores[current] + policy["hysteresis_margin"]:
            winner = current
    return {"promoted": winner if winner != control else None,
            "reason": "highest qualified accuracy after guardrails and hysteresis"}
