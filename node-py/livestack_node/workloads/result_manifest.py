"""Bounded, owner-scoped completion data referenced by workload intents."""
from __future__ import annotations

from .model import WorkloadError, encode

MAX_RESULT_MANIFEST_BYTES = 8 * 1024 * 1024
_ATTEMPT_FIELDS = ("id", "worker", "boot", "host", "fence", "state", "compilation")


def workload_result_manifest(job: dict, max_bytes: int = MAX_RESULT_MANIFEST_BYTES) -> tuple[dict, bytes]:
    """Return the verified-result payload shared by the intent ref and its fetch route.

    The status partial carries the reference. This response carries the bounded
    job/spec/result identity and only the current attempt's compiler grant, so a
    caller can verify artifacts after observing ``done`` without polling status.
    """
    if not isinstance(job, dict) or job.get("state") != "succeeded":
        raise WorkloadError("job result is not available", 409)
    spec = job.get("spec")
    completion = job.get("result")
    if (not isinstance(spec, dict) or not isinstance(completion, dict) or
            not isinstance(completion.get("result"), dict)):
        raise WorkloadError("completed job identity is unavailable", 409)

    fence = job.get("fence")
    attempts = job.get("attempts")
    latest = next((attempt for attempt in reversed(attempts or [])
                   if isinstance(attempt, dict) and attempt.get("fence") == fence), None)
    manifest = {
        "id": job.get("id"),
        "state": job["state"],
        "fence": fence,
        "spec": spec,
        "attempts": [{key: latest.get(key) for key in _ATTEMPT_FIELDS}] if latest else [],
        "result": completion,
    }
    raw = encode(manifest, max_bytes).encode("utf-8")
    return manifest, raw
