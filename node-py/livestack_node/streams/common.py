"""Shared helpers for the Harmony stream producers (services-own-their-streams).

Standard library only, and no import of the parent `livestack_node` package: benchday's isolated lane
copies this directory into a container and runs it against the real daemon, so every module here uses
relative imports within `streams/` only.
"""
from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Callable, Optional

# The one generated refusal list (benchday packages/benchday-plugin-api/scripts/stream-descriptor.mjs
# REFUSAL_CODES). Only the codes this service can itself mint are named here.
REFUSAL_SCHEMA_INVALID = "schema_invalid"
REFUSAL_OVERSIZE = "oversize"
REFUSAL_FORBIDDEN_FIELD = "forbidden_body_field"
REFUSAL_STEP_NOT_ALLOWED = "step_not_allowed"
REFUSAL_UNAVAILABLE = "authority_unavailable"
REFUSAL_INEXPRESSIBLE = "requirement_inexpressible"
REFUSAL_UNSATISFIABLE = "unsatisfiable"

# A write payload is at most one ingress frame (64 KiB) and a partial at most 4096 bytes in the catalog;
# the body an intent may carry is bounded well below that (large inputs travel by reference).
MAX_BODY_BYTES = 3072


def canonical(value: Any) -> bytes:
    """Canonical JSON bytes: sorted keys, no whitespace, UTF-8. The digest input for declarations."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def bounded(value: Any, limit: int) -> str:
    """Truncate to `limit` characters; mirrors the Stage 1 adapter's `bounded` (byte-safe for ASCII)."""
    text = "" if value is None else str(value)
    return text[:limit]


def wall_ms() -> int:
    return int(time.time() * 1000)


Clock = Callable[[], int]
Publisher = Callable[..., Any]


def body_size(body: Any) -> int:
    return len(canonical(body))


def refused(contract: str, source: str, epoch: int, seq: int, intent_id: str, code: str, detail: str,
            now_ms: int, **extra: Any) -> dict:
    """A refusal partial: status 'refused' with its code, never an empty or ok-looking answer."""
    body = {"intent_id": intent_id, "status": "refused", "refusal": code, "detail": bounded(detail, 256)}
    body.update({k: v for k, v in extra.items() if v is not None})
    return {"contract": contract, "source": source, "subject": {"intent_id": intent_id}, "epoch": epoch,
            "seq": seq, "produced_ms": now_ms, "ttl_ms": 0, "status": "ok", "body": body}


def partial(contract: str, source: str, epoch: int, seq: int, intent_id: str, body: dict, now_ms: int) -> dict:
    return {"contract": contract, "source": source, "subject": {"intent_id": intent_id}, "epoch": epoch,
            "seq": seq, "produced_ms": now_ms, "ttl_ms": 0, "status": "ok", "body": body}


def optional(clock: Optional[Clock]) -> Clock:
    return clock or wall_ms
