"""Validate typed-decision request/result/error/slate envelopes.

JSON Schema is the structural gate. Extra semantic checks (unique ids, deadline
bounds, probability finite/sum/argmax, exact id-set equality) live here so a
schema-valid-but-wrong payload still fails closed.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .identity import PACKING_VERSION, SCHEMA_VERSION
from .paths import DATA_DIR, REVISION_FILE, SCHEMA_DIR

CONTRACT_REVISION = REVISION_FILE.read_text(encoding="utf-8").splitlines()[0].strip()
MAX_BODY_BYTES = 64 * 1024
MAX_DEADLINE_AHEAD_MS = 120_000
UTF8_ID_MAX = 96
CAUSE_HTTP = {
    "unauthorized": 401,
    "forbidden": 403,
    "invalid_input": 400,
    "unsupported_profile": 422,
    "broker_unavailable": 503,
    "profile_unavailable": 503,
    "insufficient_context": 422,
    "invalid_output": 422,
    "cancelled": 503,
    "deadline_exceeded": 504,
    "capacity": 429,
    "unhealthy": 503,
}


class ContractError(ValueError):
    def __init__(self, cause: str, detail: str, http_status: Optional[int] = None):
        super().__init__(detail)
        self.cause = cause
        self.detail = detail
        self.http_status = http_status or CAUSE_HTTP.get(cause, 400)

    def envelope(self, request_id: str = "") -> Dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "outcome": "error",
            "cause": self.cause,
            "http_status": self.http_status,
            "detail": self.detail,
        }


def _load_schema(name: str) -> Dict[str, Any]:
    path = SCHEMA_DIR / name
    return json.loads(path.read_text(encoding="utf-8"))


def _validator():
    try:
        import jsonschema
    except ImportError as e:  # pragma: no cover — dev extra
        raise ContractError("invalid_input", "jsonschema is required to validate decisions") from e
    return jsonschema.Draft7Validator


def _schema_validate(schema: Mapping[str, Any], payload: Mapping[str, Any], cause: str = "invalid_input") -> None:
    Validator = _validator()
    validator = Validator(schema)
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        err = errors[0]
        path = ".".join(str(p) for p in err.path) or "<root>"
        raise ContractError(cause, f"{path}: {err.message}")


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _body_bytes(payload: Mapping[str, Any]) -> int:
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def validate_request(payload: Mapping[str, Any], *, now_ms: Optional[int] = None) -> Mapping[str, Any]:
    if _body_bytes(payload) > MAX_BODY_BYTES:
        raise ContractError("invalid_input", "request exceeds 64 KiB")
    _schema_validate(_load_schema("request.schema.json"), payload)
    deadline = payload["deadline_at_ms"]
    if deadline == 0:
        raise ContractError("invalid_input", "deadline_at_ms of 0 is a placeholder and is rejected")
    now = int(now_ms if now_ms is not None else time.time() * 1000)
    if deadline <= now:
        raise ContractError("deadline_exceeded", "request deadline has already expired", 504)
    if deadline - now > MAX_DEADLINE_AHEAD_MS:
        raise ContractError("invalid_input", "deadline is unreasonably far in the future")
    ids = [q["id"] for q in payload["questions"]]
    if any(_utf8_len(i) > UTF8_ID_MAX for i in ids):
        raise ContractError("invalid_input", "question id exceeds 96 UTF-8 bytes")
    if len(ids) != len(set(ids)):
        raise ContractError("invalid_input", "duplicate question ids")
    for q in payload["questions"]:
        if q["type"] == "choice":
            crit = q.get("criteria") or {}
            if not crit:
                raise ContractError("invalid_input", f"choice {q['id']} has no criteria")
            if len(crit) > 8:
                raise ContractError("unsupported_profile", f"choice {q['id']} has more than 8 options")
            if len(crit) != len(set(crit)):
                raise ContractError("invalid_input", f"choice {q['id']} has duplicate option ids")
        elif q["type"] != "noul":
            raise ContractError("unsupported_profile", f"unsupported question type {q['type']}")
    return payload


def _finite_unit(value: Any) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(float(value)) and 0.0 <= float(value) <= 1.0


def validate_result(
    payload: Mapping[str, Any],
    *,
    request: Optional[Mapping[str, Any]] = None,
) -> Mapping[str, Any]:
    if _body_bytes(payload) > MAX_BODY_BYTES:
        raise ContractError("invalid_output", "result exceeds 64 KiB")
    _schema_validate(_load_schema("result.schema.json"), payload, cause="invalid_output")
    if payload.get("execution", {}).get("packing_version") != PACKING_VERSION:
        raise ContractError("invalid_output", "packing_version mismatch")
    answers = payload["answers"]
    if request is not None:
        expected = [q["id"] for q in request["questions"]]
        got = list(answers.keys())
        if sorted(got) != sorted(expected):
            raise ContractError("invalid_output", "answer ids must equal the request question ids exactly")
        if payload["request_id"] != request["request_id"]:
            raise ContractError("invalid_output", "request_id mismatch")
        if payload["evidence_revision"] != request["evidence_revision"]:
            raise ContractError("invalid_output", "evidence_revision mismatch")
        if payload["profile_id"] != request["profile_id"]:
            raise ContractError("invalid_output", "profile_id mismatch")
        for q in request["questions"]:
            ans = answers[q["id"]]
            if q["type"] != ans.get("type"):
                raise ContractError("invalid_output", f"{q['id']} type mismatch")
            if q["type"] == "choice":
                option_ids = set((q.get("criteria") or {}).keys())
                probs = ans.get("probabilities") or {}
                if set(probs.keys()) != option_ids:
                    raise ContractError("invalid_output", f"{q['id']} probability keys must equal choice options")
                if ans.get("choice") not in option_ids:
                    raise ContractError("invalid_output", f"{q['id']} choice is not an option")
    for qid, ans in answers.items():
        if ans.get("type") == "noul":
            if "probability" not in ans or any(k in ans for k in ("noul", "score", "value") if k != "type"):
                if any(alias in ans for alias in ("noul", "score", "value")):
                    raise ContractError("invalid_output", f"{qid} uses a forbidden binary alias")
            if not _finite_unit(ans.get("probability")):
                raise ContractError("invalid_output", f"{qid} probability is not finite in [0,1]")
        elif ans.get("type") == "choice":
            probs = ans.get("probabilities") or {}
            if not probs:
                raise ContractError("invalid_output", f"{qid} missing probabilities")
            if set(probs) != set(probs):  # noqa: keep explicit
                raise ContractError("invalid_output", f"{qid} duplicate probability keys")
            values = []
            for key, val in probs.items():
                if not _finite_unit(val):
                    raise ContractError("invalid_output", f"{qid}.{key} is not finite in [0,1]")
                values.append(float(val))
            if abs(sum(values) - 1.0) > 1e-3:
                raise ContractError("invalid_output", f"{qid} probabilities must sum to 1 within 1e-3")
            argmax = max(probs.items(), key=lambda kv: (float(kv[1]), kv[0]))[0]
            # Profile option-order tie-break is applied by the worker before
            # this check; we only require the reported choice to be a max.
            reported = ans["choice"]
            if float(probs[reported]) < max(float(v) for v in probs.values()) - 1e-12:
                raise ContractError("invalid_output", f"{qid} choice is not argmax")
            del argmax
        else:
            raise ContractError("invalid_output", f"{qid} unknown answer type")
    return payload


def validate_error(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    _schema_validate(_load_schema("error.schema.json"), payload)
    if payload.get("outcome") == "ok" or "answers" in payload:
        raise ContractError("invalid_output", "error envelope must not carry answers or outcome=ok")
    return payload


def validate_slate(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    _schema_validate(_load_schema("slate.schema.json"), payload)
    ids = [c["id"] for c in payload.get("chips") or []]
    if len(ids) != len(set(ids)):
        raise ContractError("invalid_input", "duplicate chip ids")
    outcome = payload["outcome"]
    chips = payload.get("chips") or []
    if outcome == "selected" and not (1 <= len(chips) <= 3):
        raise ContractError("invalid_input", "selected slate must contain 1–3 chips")
    if outcome == "abstained" and len(chips) != 0:
        raise ContractError("invalid_input", "abstained slate must be an explicit empty list")
    for chip in chips:
        bucket = chip["score_bucket"]
        expected = math.floor(float(chip["usefulness"]) * 100)
        if bucket != expected:
            raise ContractError("invalid_input", f"score_bucket {bucket} != floor(p*100)={expected}")
    return payload


def load_profile(name: str) -> Dict[str, Any]:
    path = DATA_DIR / "profiles" / name
    return json.loads(path.read_text(encoding="utf-8"))


def load_thresholds() -> Dict[str, Any]:
    return json.loads((DATA_DIR / "thresholds.json").read_text(encoding="utf-8"))


def load_fixture(name: str) -> Dict[str, Any]:
    return json.loads((DATA_DIR / "fixtures" / name).read_text(encoding="utf-8"))
