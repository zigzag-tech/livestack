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


def input_objects(value) -> list[dict]:
    if not isinstance(value, list) or not 1 <= len(value) <= 128:
        raise WorkloadError("input_objects must contain between one and 128 objects")
    result = []
    names = set()
    total = 0
    for item in value:
        if not isinstance(item, dict) or set(item) != {"name", "digest", "size"}:
            raise WorkloadError("invalid input object")
        object_name = item.get("name")
        if (not isinstance(object_name, str) or len(object_name) > 240 or object_name.startswith("/")
                or any(part in ("", ".", "..") for part in object_name.split("/"))
                or not re.fullmatch(r"[A-Za-z0-9_.@+-]+(?:/[A-Za-z0-9_.@+-]+)*", object_name)):
            raise WorkloadError("invalid input object name")
        digest = item.get("digest")
        size = item.get("size")
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise WorkloadError("input object digest must be SHA-256")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= 2 * 1024**3:
            raise WorkloadError("invalid input object size")
        if object_name in names:
            raise WorkloadError("duplicate input object name")
        names.add(object_name)
        total += size
        if total > 4 * 1024**3:
            raise WorkloadError("input objects exceed total byte limit")
        result.append(dict(name=object_name, digest=digest, size=size))
    return result


def submission(value: dict, handlers: set[str], limits: Limits) -> dict:
    if not isinstance(value, dict):
        raise WorkloadError("submission must be an object")
    allowed = {"version", "key", "handler", "input_digest", "input_objects", "payload", "need",
               "admit", "selector", "estimate_seconds", "deadline", "priority", "locality_host", "retain"}
    version = value.get("version")
    if set(value) - allowed or version not in (1, 2) or (version == 1 and "input_objects" in value):
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
    # Admission and execution are separate quantities: `admit` is what must be
    # free on a target before work starts, `need` is what the attempt may use.
    # A job that only requires two cores to make progress can still be allowed
    # to burst, so a host whose whole capacity equals `need` is not excluded.
    admit = None
    if value.get("admit") is not None:
        admit = resources(value.get("admit"))
        if set(admit) - set(need):
            raise WorkloadError("admit may only constrain dimensions that need declares")
        if any(quantity > need[key] for key, quantity in admit.items()):
            raise WorkloadError("admit must not exceed need in any dimension")
    estimate = value.get("estimate_seconds", 3600)
    if isinstance(estimate, bool) or not isinstance(estimate, (int, float)) or not 0 < estimate <= 86400:
        raise WorkloadError("estimate_seconds must be in (0, 86400]")
    deadline = value.get("deadline")
    if deadline is not None and (isinstance(deadline, bool)
            or not isinstance(deadline, (float, int)) or not math.isfinite(deadline) or deadline <= 0):
        raise WorkloadError("deadline must be a finite epoch time")
    priority = value.get("priority")
    if priority is not None and (isinstance(priority, bool) or not isinstance(priority, int)
                                 or not 0 <= priority <= 1_000_000):
        raise WorkloadError("priority must be an integer in [0, 1000000]")
    if not isinstance(value.get("payload", {}), dict) or not isinstance(value.get("retain", False), bool):
        raise WorkloadError("invalid payload or retain flag")
    result = dict(version=version, key=name(value.get("key"), "key"), handler=handler,
                  input_digest=digest, payload=value.get("payload", {}), need=need,
                  selector=labels(value.get("selector", {})), estimate_seconds=estimate,
                  deadline=deadline, locality_host=value.get("locality_host"),
                  retain=value.get("retain", False))
    # Preserve the canonical bytes of legacy idempotency requests that omitted
    # priority. Placement treats an absent field as zero.
    if priority is not None:
        result["priority"] = priority
    # Absent means "admit at need", which is the behavior every existing caller
    # already has; keeping the key out preserves their idempotency bytes.
    if admit is not None:
        result["admit"] = admit
    if version == 2:
        result["input_objects"] = input_objects(value.get("input_objects", []))
    if result["locality_host"] is not None:
        name(result["locality_host"], "locality_host")
    encode(result, limits.record_bytes)
    return result


def identity(value: dict) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()
