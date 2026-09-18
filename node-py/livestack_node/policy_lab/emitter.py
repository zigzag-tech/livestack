"""Fail-open, metadata-only observation queue and bounded local spool."""

from __future__ import annotations

import json
import os
from collections import deque
from pathlib import Path
from typing import Any

from .contracts import ContractError


ALLOWED_METADATA_FIELDS = frozenset(
    {
        "request_id",
        "attempt_id",
        "decision_id",
        "workflow_id",
        "parent_request_id",
        "principal_id",
        "application",
        "workload_class",
        "operation",
        "objective_profile_id",
        "worker_id",
        "model_revision",
        "engine_revision",
        "hardware_revision",
        "route_id",
        "source_region",
        "destination_region",
        "permitted_regions",
        "voice_compatibility_id",
        "embedding_space_id",
        "input_bytes",
        "input_tokens",
        "output_tokens",
        "audio_duration_us",
        "produced_units",
        "received_units",
        "acknowledged_units",
        "coverage_us",
        "buffer_duration_us",
        "stall_duration_us",
        "deadline_us",
        "reason_code",
        "outcome",
        "failure_class",
        "sampling_probability",
        "client_cancel",
        "system_cancel",
        "fallback_kind",
        "observation_age_us",
    }
)
FORBIDDEN_TEXT_MARKERS = (
    "://",
    "bearer ",
    "authorization:",
    "-----begin ",
)


def _safe_value(value: Any, path: str) -> Any:
    if value is None or type(value) in (bool, int, float):
        return value
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        lowered = value.lower()
        if len(encoded) > 1024 or any(marker in lowered for marker in FORBIDDEN_TEXT_MARKERS):
            raise ContractError(f"unsafe metadata string at {path}")
        return value
    if isinstance(value, list):
        if len(value) > 128:
            raise ContractError(f"metadata list too large at {path}")
        return [_safe_value(item, f"{path}[]") for item in value]
    raise ContractError(f"unsupported metadata value at {path}")


def sanitize_metadata(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ContractError("metadata payload must be an object")
    unknown = payload.keys() - ALLOWED_METADATA_FIELDS
    if unknown:
        raise ContractError(f"metadata payload has unknown fields: {sorted(unknown)}")
    return {key: _safe_value(payload[key], key) for key in sorted(payload)}


class BoundedEmitter:
    """A no-I/O producer queue; observation failure never blocks service."""

    def __init__(self, *, max_queue_bytes: int, max_event_bytes: int) -> None:
        if max_queue_bytes <= 0 or max_event_bytes <= 0:
            raise ValueError("emitter bounds must be positive")
        self.max_queue_bytes = max_queue_bytes
        self.max_event_bytes = max_event_bytes
        self._queue: deque[bytes] = deque()
        self._queue_bytes = 0
        self._gap_count = 0
        self._gap_first_us: int | None = None
        self._gap_last_us: int | None = None
        self._gap_types: set[str] = set()
        self.dropped_events = 0
        self.rejected_events = 0
        self.producer_io_calls = 0

    @staticmethod
    def _encode(value: Any) -> bytes:
        return (
            json.dumps(
                value,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            + b"\n"
        )

    def _record_drop(self, event_type: str, now_us: int) -> None:
        self.dropped_events += 1
        self._gap_count += 1
        self._gap_first_us = now_us if self._gap_first_us is None else self._gap_first_us
        self._gap_last_us = now_us
        self._gap_types.add(event_type)

    def _enqueue(self, encoded: bytes) -> bool:
        if len(encoded) > self.max_event_bytes:
            return False
        if self._queue_bytes + len(encoded) > self.max_queue_bytes:
            return False
        self._queue.append(encoded)
        self._queue_bytes += len(encoded)
        return True

    def _flush_gap_if_possible(self) -> None:
        if self._gap_count == 0:
            return
        encoded = self._encode(
            {
                "schema_version": 1,
                "kind": "evidence_gap",
                "dropped_count": self._gap_count,
                "first_us": self._gap_first_us,
                "last_us": self._gap_last_us,
                "event_types": sorted(self._gap_types),
            }
        )
        if self._enqueue(encoded):
            self._gap_count = 0
            self._gap_first_us = None
            self._gap_last_us = None
            self._gap_types.clear()

    def offer(self, event_type: str, payload: Any, *, now_us: int) -> bool:
        """Attempt enqueue only; catches all evidence errors and performs no I/O."""

        try:
            if not isinstance(event_type, str) or not event_type:
                raise ContractError("event_type must be a non-empty string")
            if type(now_us) is not int or now_us < 0:
                raise ContractError("now_us must be a nonnegative integer")
            clean = sanitize_metadata(payload)
            encoded = self._encode(
                {
                    "schema_version": 1,
                    "kind": "observation_payload",
                    "event_type": event_type,
                    "observed_at_us": now_us,
                    "payload": clean,
                }
            )
        except (ContractError, TypeError, ValueError, UnicodeError):
            self.rejected_events += 1
            return False
        self._flush_gap_if_possible()
        if not self._enqueue(encoded):
            self._record_drop(event_type, now_us)
            return False
        return True

    def drain(self, *, max_records: int | None = None) -> list[bytes]:
        if max_records is None:
            max_records = len(self._queue)
        result: list[bytes] = []
        while self._queue and len(result) < max_records:
            encoded = self._queue.popleft()
            self._queue_bytes -= len(encoded)
            result.append(encoded)
        return result


class SegmentedSpool:
    """Dedicated bounded spool; called only by a consumer/drain worker."""

    def __init__(self, root: Path, *, max_bytes: int, segment_bytes: int) -> None:
        if max_bytes <= 0 or segment_bytes <= 0 or segment_bytes > max_bytes:
            raise ValueError("invalid spool bounds")
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.segment_bytes = segment_bytes
        self.refused_records = 0
        self.root.mkdir(parents=True, exist_ok=True)

    def _segments(self) -> list[Path]:
        return sorted(self.root.glob("segment-*.jsonl"))

    @property
    def used_bytes(self) -> int:
        return sum(path.stat().st_size for path in self._segments())

    def append(self, encoded: bytes) -> bool:
        if not isinstance(encoded, bytes) or not encoded.endswith(b"\n"):
            raise ContractError("spool records must be newline-terminated bytes")
        if len(encoded) > self.segment_bytes or self.used_bytes + len(encoded) > self.max_bytes:
            self.refused_records += 1
            return False
        segments = self._segments()
        target = segments[-1] if segments else self.root / "segment-000000.jsonl"
        current_size = target.stat().st_size if target.exists() else 0
        if current_size + len(encoded) > self.segment_bytes:
            index = int(target.stem.split("-")[-1]) + 1
            target = self.root / f"segment-{index:06d}.jsonl"
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return True
