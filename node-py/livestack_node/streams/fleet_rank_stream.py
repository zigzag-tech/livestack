"""Bounded per-vantage fleet rankings for the native Harmony stream producer."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Callable, Dict, List, Sequence, Tuple

from .. import fleet_rank
from ..preferences import parse_preference_list

CONTRACT = "harmony.fleet-rank/1"
MAX_KEYS = 32
MAX_TARGETS = 64
MAX_BODY_BYTES = 28 * 1024  # leave room for the partial envelope under the 32 KiB contract cap
DEFAULT_TTL_S = 60.0
DEFAULT_INTERVAL_S = 30
MIN_INTERVAL_S = 5
MAX_INTERVAL_S = 60
MAX_REASON_LENGTH = 256
REASON_TRUNCATION = "...[truncated]"

ASR_PREFERENCES = [
    {"attribute": "asr.streaming", "value": "native"},
    {"metric": "asr.first_partial_ms", "direction": "min", "min_samples": 10},
    {"metric": "asr.finalization_ms", "direction": "min", "min_samples": 10},
]
TARGET_FIELDS = (
    "target_id", "host_id", "distance_ms", "distance_band", "load", "rank",
    "reason", "inventory", "preferences",
)


class RankingFailure(ValueError):
    """A named producer refusal; never a successful empty ordering."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail.encode("utf-8")[:256].decode("utf-8", errors="ignore")


def _bounded_json_value(value: Any, depth: int) -> bool:
    """Match the catalog's recursively bounded JSON value definitions."""
    if value is None or isinstance(value, bool):
        return True
    if isinstance(value, str):
        return len(value) <= 512
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if depth <= 0:
        return False
    if isinstance(value, list):
        return len(value) <= 32 and all(_bounded_json_value(item, depth - 1) for item in value)
    if isinstance(value, dict):
        return (len(value) <= 32
                and all(isinstance(key, str) and len(key) <= 64
                        and _bounded_json_value(item, depth - 1)
                        for key, item in value.items()))
    return False


def _valid_preference_receipt(receipt: Any) -> bool:
    if not isinstance(receipt, dict) or not {"clause", "comparable"} <= set(receipt):
        return False
    if set(receipt) - {"clause", "value", "comparable", "matched", "reason"}:
        return False
    if not isinstance(receipt["clause"], dict) or not isinstance(receipt["comparable"], bool):
        return False
    if not _bounded_json_value(receipt["clause"], 3):
        return False
    if "value" in receipt and not _bounded_json_value(receipt["value"], 3):
        return False
    if "matched" in receipt and not isinstance(receipt["matched"], bool):
        return False
    return "reason" not in receipt or (isinstance(receipt["reason"], str) and len(receipt["reason"]) <= 128)


def parse_pairs(raw: str, log: Callable[[str], None] = print) -> List[Tuple[str, str, str]]:
    """Read bounded `(kind, region, vantage)` keys, preserving valid siblings and order."""
    if len(raw.encode("utf-8")) > 32 * 1024:
        log("streams: fleet-rank configuration exceeds 32768 bytes; contract disabled")
        return []
    out: List[Tuple[str, str, str]] = []
    seen_keys = set()
    refused_keys = 0
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        lhs, separator, vantage = entry.partition("=")
        kind, colon, region = lhs.partition(":")
        kind, region, vantage = kind.strip(), region.strip(), vantage.strip()
        fields = (kind, region, vantage)
        if (not separator or not colon or entry.count("=") != 1 or lhs.count(":") != 1
                or any(not value for value in fields)):
            log(f"streams: skipping malformed fleet-rank pair {entry!r}")
            continue
        if (len(kind.encode()) > 32 or len(region.encode()) > 32 or len(vantage.encode()) > 128
                or any(ord(char) < 0x20 for value in fields for char in value)):
            log(f"streams: skipping fleet-rank pair outside field bounds {entry!r}")
            continue
        triple = (kind, region, vantage)
        if triple in seen_keys:
            log(f"streams: skipping duplicate fleet-rank key {entry!r}")
            continue
        seen_keys.add(triple)
        if len(out) >= MAX_KEYS:
            refused_keys += 1
            continue
        out.append(triple)
    if refused_keys:
        log(f"streams: fleet_rank_key_limit: refused {refused_keys} configured keys beyond {MAX_KEYS}; "
            f"the first {MAX_KEYS} remain active")
    return out


def interval_seconds(raw: str | None, log: Callable[[str], None] = print) -> int:
    if raw is None or not raw.strip():
        return DEFAULT_INTERVAL_S
    try:
        value = int(raw)
    except (TypeError, ValueError):
        log(f"streams: invalid BENCHDAY_FLEET_RANK_SECONDS={raw!r}; using {DEFAULT_INTERVAL_S}")
        return DEFAULT_INTERVAL_S
    if MIN_INTERVAL_S <= value <= MAX_INTERVAL_S:
        return value
    log(f"streams: BENCHDAY_FLEET_RANK_SECONDS must be {MIN_INTERVAL_S}..{MAX_INTERVAL_S}; using {DEFAULT_INTERVAL_S}")
    return DEFAULT_INTERVAL_S


def ranking_body(view: dict, triple: Tuple[str, str, str], now_s: float) -> Dict[str, Any]:
    """Run the existing ranker and preserve the daemon's complete ranking shape."""
    kind, region, vantage = triple
    prefer = parse_preference_list(ASR_PREFERENCES if kind == "asr" else [])
    try:
        result = fleet_rank.rank(view, kind, vantage=vantage, now=now_s,
                                 ttl_s=DEFAULT_TTL_S, prefer=prefer)
    except Exception as error:  # noqa: BLE001 - converted to a named failed partial by the caller
        raise RankingFailure("fleet_rank_compute_failed", type(error).__name__) from error
    if not isinstance(result, dict):
        raise RankingFailure("fleet_rank_invalid_result", "ranker returned no result object")
    targets = result.get("targets")
    if not isinstance(targets, list):
        raise RankingFailure("fleet_rank_invalid_result", "ranker returned no target list")
    if len(targets) > MAX_TARGETS:
        raise RankingFailure("fleet_rank_target_limit", f"{len(targets)} targets exceeds {MAX_TARGETS}")
    if any(not isinstance(target, dict) or not isinstance(target.get("target_id"), str)
           for target in targets):
        raise RankingFailure("fleet_rank_invalid_target", "ranker returned a target without target_id")
    generated_at = result.get("generated_at")
    chosen = result.get("chosen")
    reason = result.get("reason")
    if (result.get("kind") != kind or result.get("vantage") != vantage
            or result.get("vantage_used") != vantage
            or isinstance(generated_at, bool) or not isinstance(generated_at, (int, float))
            or not math.isfinite(generated_at) or generated_at <= 0
            or (targets and (not isinstance(chosen, str) or not chosen
                             or len(chosen.encode("utf-8")) > 256
                             or chosen != targets[0].get("target_id")))
            or (not targets and chosen is not None)
            or not isinstance(reason, str)):
        raise RankingFailure("fleet_rank_invalid_result", "ranker returned an incomplete ordering")
    if not targets:
        raise RankingFailure("fleet_rank_empty", f"no ranked targets for {kind} from {vantage}")
    if len(reason) > MAX_REASON_LENGTH:
        # The contract bounds this human-readable summary. Per-target reasons
        # remain in `targets`; mark the summary cut so the bound is visible.
        reason = reason[:MAX_REASON_LENGTH - len(REASON_TRUNCATION)] + REASON_TRUNCATION
    prefer = result.get("prefer", [])
    if not isinstance(prefer, list) or len(prefer) > 8 or not all(
            isinstance(clause, dict) and _bounded_json_value(clause, 3) for clause in prefer):
        raise RankingFailure("fleet_rank_invalid_result", "ranker returned invalid preferences")
    bounded_targets = []
    for index, target in enumerate(targets):
        target_id = target["target_id"]
        host_id = target.get("host_id")
        distance = target.get("distance_ms")
        band = target.get("distance_band")
        load = target.get("load")
        rank = target.get("rank")
        target_reason = target.get("reason")
        inventory = target.get("inventory")
        receipts = target.get("preferences")
        if (not target_id or len(target_id.encode("utf-8")) > 256
                or (host_id is not None and (not isinstance(host_id, str) or len(host_id) > 128))
                or (distance is not None and (isinstance(distance, bool) or not isinstance(distance, (int, float))
                                              or not math.isfinite(distance) or distance < 0))
                or (band is not None and (not isinstance(band, str) or len(band) > 16))
                or (load is not None and (not isinstance(load, dict) or len(load) > 16
                                          or not _bounded_json_value(load, 2)))
                or isinstance(rank, bool) or not isinstance(rank, int) or not 1 <= rank <= MAX_TARGETS
                or not isinstance(target_reason, str) or len(target_reason) > 256
                or (inventory is not None and (not isinstance(inventory, dict)
                                               or not _bounded_json_value(inventory, 3)))
                or not isinstance(receipts, list) or len(receipts) > 8
                or not all(_valid_preference_receipt(receipt) for receipt in receipts)):
            raise RankingFailure("fleet_rank_invalid_target", f"ranker returned invalid target at index {index}")
        bounded_targets.append({field: target.get(field) for field in TARGET_FIELDS})
    decision_material = {
        "kind": kind,
        "region": region,
        "generated_at": float(generated_at),
        "targets": bounded_targets,
        "prefer": prefer,
    }
    try:
        canonical_decision = json.dumps(
            decision_material, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise RankingFailure("fleet_rank_invalid_result", "ranker returned non-JSON decision data") from error
    decision_id = "fr_" + hashlib.sha256(canonical_decision).hexdigest()
    body = {
        "state": "present",
        "kind": kind,
        "region": region,
        "vantage": vantage,
        "generated_at": float(generated_at),
        "ttl_s": DEFAULT_TTL_S,
        "decision_id": decision_id,
        "reason": reason,
        "targets": bounded_targets,
        "prefer": prefer,
    }
    try:
        encoded = json.dumps(body, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as error:
        raise RankingFailure("fleet_rank_invalid_result", "ranker returned non-JSON ranking data") from error
    if len(encoded) > MAX_BODY_BYTES:
        raise RankingFailure("fleet_rank_oversize", f"encoded ranking is {len(encoded)} bytes; cap is {MAX_BODY_BYTES}")
    return body


def removal_body(triple: Tuple[str, str, str], reason: str = "no_targets") -> Dict[str, Any]:
    """Remove one keyed vantage without replacing it with a successful empty ordering."""
    kind, region, vantage = triple
    if reason not in ("no_targets", "operator_removed"):
        raise ValueError("fleet_rank_removal_reason_invalid")
    return {
        "state": "removed",
        "kind": kind,
        "region": region,
        "vantage": vantage,
        "removed_reason": reason,
    }
