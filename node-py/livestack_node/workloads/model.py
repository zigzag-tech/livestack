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


class ArtifactTooLarge(WorkloadError):
    """A result artifact is over this worker's upload bound. Retrying cannot
    shrink it, so it is terminal for the attempt (never a transient fault)."""

    def __init__(self, size: int, limit: int):
        super().__init__('upload byte limit exceeded: artifact is %d bytes, limit is %d bytes' % (size, limit), 413)
        self.size, self.limit = size, limit


@dataclass(frozen=True)
class Limits:
    active_jobs: int = 1000
    terminal_jobs: int = 10000
    workers: int = 128
    claims_per_worker: int = 32
    # Infrastructure outcomes retry once (two attempts in all). Measured
    # 2026-09-30: three attempts on a starved worker cost 41-99 minutes of wall
    # for nothing; a second look is worth it, a third rarely is.
    attempts: int = 2
    record_bytes: int = 65536
    fresh_seconds: float = 60
    lease_seconds: float = 120
    # How long a cleanup hold may outlive the worker that owes its
    # acknowledgement. Generous against an ordinary worker restart, bounded
    # against a worker that never returns -- see `WorkloadStore._expire`.
    cleanup_seconds: float = 3600
    terminal_seconds: float | None = 14 * 86400
    # Persistent task environments are disk-only acceleration state. These
    # bounds are independent of the job/attempt limits above: a parked
    # environment never consumes a running-job slot.
    environment_registry: int = 1024
    environments_per_owner: int = 64
    environment_idle_seconds: float | None = 7 * 86400
    environment_generation_seconds: float | None = 30 * 86400
    environment_affinity_seconds: float = 15
    environment_sweep_rows: int = 64
    environment_sweep_seconds: float = 5

    def __post_init__(self):
        for name in ("active_jobs", "terminal_jobs", "workers", "claims_per_worker",
                     "attempts", "record_bytes", "fresh_seconds", "lease_seconds",
                     "cleanup_seconds", "environment_registry", "environments_per_owner",
                     "environment_affinity_seconds", "environment_sweep_rows", "environment_sweep_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.attempts > 3:
            raise ValueError("attempts cannot exceed three")
        if self.terminal_seconds is not None and (
                not math.isfinite(self.terminal_seconds) or self.terminal_seconds <= 0):
            raise ValueError("terminal_seconds must be positive or None (deletion disabled)")
        for field in ("environment_idle_seconds", "environment_generation_seconds"):
            value = getattr(self, field)
            # An unset or zero environment retention window disables destructive
            # expiry. New environment admission then fails closed in the store.
            if value is not None and (not math.isfinite(value) or value < 0):
                raise ValueError(f"{field} must be positive, zero, or None")
        if self.environment_registry > 1024 or self.environments_per_owner > 64 or self.environment_sweep_rows > 64:
            raise ValueError("environment limits may only be lowered from their hard ceilings")
        if self.environment_affinity_seconds > 15:
            raise ValueError("environment affinity may not exceed its 15 second hard ceiling")


AVOID_LABEL_WORKER = "harmony.avoid.worker"
AVOID_LABEL_SIGNATURE = "harmony.avoid.signature"


def failure_signature(record) -> str | None:
    """A stable, short name for WHY an infrastructure attempt ended, or None.

    `<tag>-<hash8>`: the tag is the worker's own error name (or `exit<code>`),
    the hash covers the error, detail and exit code with digits collapsed and
    only the first line kept, so a timestamp or pid in a message does not make
    two identical failures look different. Bounded: at most 41 characters.
    """
    if not isinstance(record, dict) or record.get("outcome") != "infrastructure":
        return None
    result = record.get("result")
    result = result if isinstance(result, dict) else {}
    error, detail, code = result.get("error"), result.get("detail"), result.get("exit_code")
    tag = str(error) if error else (f"exit{code}" if code is not None else "unknown")
    tag = re.sub(r"[^A-Za-z0-9_.]", "_", tag)[:32]
    first_line = (str(detail or "").strip().splitlines() or [""])[0]
    normal = re.sub(r"\d+", "#", f"{error}|{first_line}|{code}".lower())[:200]
    return f"{tag}-{hashlib.sha256(normal.encode()).hexdigest()[:8]}"


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


def _number(value, field, *, nullable=False):
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or value < 0 or value > 1e18:
        raise WorkloadError(f"invalid host report field: {field}")
    return value


def host_view(value) -> dict:
    """A worker's measured host block (livestack_node.hostview). Closed and bounded.

    Readings the worker could not take are null and stay null: unknown is never zero.
    """
    keys = {"memory_total_bytes", "memory_available_bytes", "memory_reserve_bytes",
            "swap_in_bytes_per_second", "psi", "attempts", "services"}
    if not isinstance(value, dict) or set(value) != keys:
        raise WorkloadError("invalid host report")
    out = {k: _number(value[k], k) for k in ("memory_total_bytes", "memory_available_bytes",
                                              "memory_reserve_bytes")}
    out["swap_in_bytes_per_second"] = _number(value["swap_in_bytes_per_second"], "swap_in", nullable=True)
    psi = value["psi"]
    windows = {"some_avg10", "some_avg60", "full_avg10", "full_avg60"}
    if not isinstance(psi, dict) or set(psi) != {"memory", "io", "cpu"}:
        raise WorkloadError("invalid host report psi")
    out["psi"] = {}
    for resource, reading in psi.items():
        if reading is not None and (not isinstance(reading, dict) or set(reading) != windows):
            raise WorkloadError("invalid host report psi")
        out["psi"][resource] = None if reading is None else {
            w: _number(reading[w], "psi", nullable=True) for w in sorted(windows)}
    attempts, services = value["attempts"], value["services"]
    if not isinstance(attempts, dict) or len(attempts) > 64 or not isinstance(services, dict) or len(services) > 32:
        raise WorkloadError("invalid host report tenants")
    out["attempts"] = {}
    for attempt, current in attempts.items():
        if not isinstance(attempt, str) or not re.fullmatch(r"[0-9a-f]{32}", attempt):
            raise WorkloadError("invalid host report attempt id")
        out["attempts"][attempt] = _number(current, "attempt memory")
    out["services"] = {}
    for service, reading in services.items():
        # `resident` (optional): True = every unit loaded, so no load spike can
        # recur; False or null (unreadable) = a spike may come and is charged.
        if (not isinstance(service, str) or not 0 < len(service) <= 256 or not isinstance(reading, dict)
                or not {"current_bytes", "peak_bytes"} <= set(reading) <= {"current_bytes", "peak_bytes", "resident"}
                or reading.get("resident") not in (True, False, None)):
            raise WorkloadError("invalid host report service")
        out["services"][service] = {k: _number(reading[k], "service memory") for k in ("current_bytes", "peak_bytes")}
        if "resident" in reading:
            out["services"][service]["resident"] = reading["resident"]
    return out


def labels(value) -> dict:
    if not isinstance(value, dict) or len(value) > 64:
        raise WorkloadError("invalid labels")
    for key, val in value.items():
        name(key, "label")
        if not isinstance(val, str) or len(val) > 256:
            raise WorkloadError("invalid label value")
    return dict(value)


def progress(value) -> dict:
    """The jingway JobProgressSchema shape: {phase, detail?, fraction?}."""
    if not isinstance(value, dict) or set(value) - {"phase", "detail", "fraction"}:
        raise WorkloadError("progress must carry phase and optional detail/fraction")
    name(value.get("phase"), "progress phase")
    result = {"phase": value["phase"]}
    detail = value.get("detail")
    if detail is not None:
        if not isinstance(detail, str) or len(detail) > 1024:
            raise WorkloadError("invalid progress detail")
        result["detail"] = detail
    fraction = value.get("fraction")
    if fraction is not None:
        if (isinstance(fraction, bool) or not isinstance(fraction, (float, int))
                or not math.isfinite(fraction) or not 0 <= fraction <= 1):
            raise WorkloadError("progress fraction must be in [0, 1]")
        result["fraction"] = float(fraction)
    return result


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
               "admit", "selector", "labels", "estimate_seconds", "deadline", "priority",
               "locality_host", "retain", "environment", "handler_release"}
    version = value.get("version")
    if set(value) - allowed or version not in (1, 2, 3) or (version == 1 and "input_objects" in value) \
            or (version != 3 and "environment" in value):
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
    job_labels = labels(value.get("labels", {}))
    if len(job_labels) > 16:
        raise WorkloadError("a submission carries at most 16 labels")
    result = dict(version=version, key=name(value.get("key"), "key"), handler=handler,
                  input_digest=digest, payload=value.get("payload", {}), need=need,
                  selector=labels(value.get("selector", {})), estimate_seconds=estimate,
                  deadline=deadline, locality_host=value.get("locality_host"),
                  retain=value.get("retain", False))
    if "handler_release" in value:
        release = value['handler_release']
        if not isinstance(release, dict) or release.get('selection') not in ('default', 'exact'):
            raise WorkloadError('handler_release_intent_invalid')
        if release['selection'] == 'default' and set(release) != {'selection'}:
            raise WorkloadError('handler_release_intent_invalid')
        if release['selection'] == 'exact':
            digest_value = release.get('release_digest')
            if (set(release) != {'selection', 'release_digest'} or not isinstance(digest_value, str) or
                    not re.fullmatch(r'[0-9a-f]{64}', digest_value)):
                raise WorkloadError('handler_release_intent_invalid')
        result['handler_release_intent'] = dict(release)
    # `labels.owner` is reserved for the end user's owner string and is
    # authorized at the HTTP boundary against the caller's delegate_prefix,
    # exactly as /fleet/admit refuses an owner outside a delegating principal.
    # Keeping the key out when absent preserves legacy idempotency bytes.
    if job_labels:
        result["labels"] = job_labels
    # Preserve the canonical bytes of legacy idempotency requests that omitted
    # priority. Placement treats an absent field as zero.
    if priority is not None:
        result["priority"] = priority
    # Absent means "admit at need", which is the behavior every existing caller
    # already has; keeping the key out preserves their idempotency bytes.
    if admit is not None:
        result["admit"] = admit
    if version == 2 or (version == 3 and "input_objects" in value):
        result["input_objects"] = input_objects(value.get("input_objects", []))
    if version == 3 and "environment" in value:
        reference = value["environment"]
        if (not isinstance(reference, dict) or set(reference) != {"reuse", "key"} and
                set(reference) != {"reuse", "handle"} or reference.get("reuse") != "prefer"):
            raise WorkloadError("environment must name exactly one key or handle with reuse=prefer")
        if "key" in reference:
            result["environment"] = {"key": name(reference["key"], "environment key"), "reuse": "prefer"}
        else:
            handle = reference.get("handle")
            if not isinstance(handle, str) or not re.fullmatch(r"[a-f0-9]{32}", handle):
                raise WorkloadError("invalid environment handle")
            result["environment"] = {"handle": handle, "reuse": "prefer"}
    if result["locality_host"] is not None:
        name(result["locality_host"], "locality_host")
    encode(result, limits.record_bytes)
    return result


def identity(value: dict) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()
