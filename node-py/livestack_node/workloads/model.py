"""Wire validation and explicit storage limits for the workload service."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import re


class WorkloadError(ValueError):
    """A client-visible refusal; never permission to run without admission."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Limits:
    active_jobs: int = 1000
    terminal_jobs: int = 10000
    workers: int = 128
    claims_per_worker: int = 32
    attempts: int = 3
    record_bytes: int = 65536
    fresh_seconds: float = 60
    lease_seconds: float = 120
    terminal_seconds: float | None = 14 * 86400

    def __post_init__(self):
        for name in ("active_jobs", "terminal_jobs", "workers", "claims_per_worker",
                     "attempts", "record_bytes", "fresh_seconds", "lease_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.attempts > 3:
            raise ValueError("attempts cannot exceed three")
        if self.terminal_seconds is not None and (
                not math.isfinite(self.terminal_seconds) or self.terminal_seconds <= 0):
            raise ValueError("terminal_seconds must be positive or None (deletion disabled)")


def encode(value, limit: int = 65536) -> str:
    try:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise WorkloadError("invalid JSON value") from exc
    if len(text.encode()) > limit:
        raise WorkloadError("record byte limit exceeded", 413)
    return text


def name(value, field: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_./:@-]{1,160}", value):
        raise WorkloadError(f"invalid {field}")
    return value


def resources(value) -> dict:
    if not isinstance(value, dict) or not value or len(value) > 32:
        raise WorkloadError("resources must be a nonempty vector of at most 32 dimensions")
    result = {}
    for key, number in value.items():
        name(key, "resource dimension")
        if (isinstance(number, bool) or not isinstance(number, (float, int))
                or not math.isfinite(number) or number < 0 or number > 1e18):
            raise WorkloadError(f"invalid resource quantity: {key}")
        result[key] = float(number)
    return result


def labels(value) -> dict:
    if not isinstance(value, dict) or len(value) > 64:
        raise WorkloadError("invalid labels")
    for key, val in value.items():
        name(key, "label")
        if not isinstance(val, str) or len(val) > 256:
            raise WorkloadError("invalid label value")
    return dict(value)


def submission(value: dict, handlers: set[str], limits: Limits) -> dict:
    if not isinstance(value, dict):
        raise WorkloadError("submission must be an object")
    allowed = {"version", "key", "handler", "input_digest", "payload", "need",
               "selector", "estimate_seconds", "deadline", "locality_host", "retain"}
    if set(value) - allowed or value.get("version") != 1:
        raise WorkloadError("unsupported workload schema or fields")
    handler = name(value.get("handler"), "handler")
    if handler not in handlers:
        raise WorkloadError("handler is not authorized", 403)
    digest = value.get("input_digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise WorkloadError("input_digest must be SHA-256")
    need = resources(value.get("need"))
    if not any(n > 0 for n in need.values()):
        raise WorkloadError("at least one resource must be requested")
    estimate = value.get("estimate_seconds", 3600)
    if isinstance(estimate, bool) or not isinstance(estimate, (int, float)) or not 0 < estimate <= 86400:
        raise WorkloadError("estimate_seconds must be in (0, 86400]")
    deadline = value.get("deadline")
    if deadline is not None and (isinstance(deadline, bool)
            or not isinstance(deadline, (float, int)) or not math.isfinite(deadline) or deadline <= 0):
        raise WorkloadError("deadline must be a finite epoch time")
    if not isinstance(value.get("payload", {}), dict) or not isinstance(value.get("retain", False), bool):
        raise WorkloadError("invalid payload or retain flag")
    result = dict(version=1, key=name(value.get("key"), "key"), handler=handler,
                  input_digest=digest, payload=value.get("payload", {}), need=need,
                  selector=labels(value.get("selector", {})), estimate_seconds=estimate,
                  deadline=deadline, locality_host=value.get("locality_host"), retain=value.get("retain", False))
    if result["locality_host"] is not None:
        name(result["locality_host"], "locality_host")
    encode(result, limits.record_bytes)
    return result


def identity(value: dict) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()
