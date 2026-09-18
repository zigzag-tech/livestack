"""Small deterministic replay used to enforce the offline package boundary."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any


class SmokeReplayError(ValueError):
    """Raised when the M0 smoke manifest is invalid."""


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def replay_smoke(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Replay a bounded list of synthetic events without external I/O.

    This is deliberately only the M0 import/replay probe.  The full simulator
    owns causal state, resource accounting, and policy execution in later
    modules.  Keeping this probe tiny makes accidental CUDA/runtime imports
    visible immediately.
    """

    if set(manifest) != {"schema_version", "events"}:
        raise SmokeReplayError("manifest fields must be schema_version and events")
    if manifest["schema_version"] != 1:
        raise SmokeReplayError("unsupported schema_version")
    events = manifest["events"]
    if not isinstance(events, Sequence) or isinstance(events, (str, bytes)):
        raise SmokeReplayError("events must be a sequence")
    if len(events) > 10_000:
        raise SmokeReplayError("event limit exceeded")

    normalized: list[dict[str, Any]] = []
    for index, event in enumerate(events):
        if not isinstance(event, Mapping):
            raise SmokeReplayError(f"event {index} must be an object")
        if set(event) != {"event_id", "virtual_time_us", "event_phase"}:
            raise SmokeReplayError(f"event {index} has invalid fields")
        event_id = event["event_id"]
        virtual_time_us = event["virtual_time_us"]
        event_phase = event["event_phase"]
        if not isinstance(event_id, str) or not event_id:
            raise SmokeReplayError(f"event {index} has invalid event_id")
        if type(virtual_time_us) is not int or virtual_time_us < 0:
            raise SmokeReplayError(f"event {index} has invalid virtual_time_us")
        if type(event_phase) is not int or not 0 <= event_phase <= 3:
            raise SmokeReplayError(f"event {index} has invalid event_phase")
        normalized.append(dict(event))

    ordered = sorted(
        enumerate(normalized),
        key=lambda item: (
            item[1]["virtual_time_us"],
            item[1]["event_phase"],
            item[0],
        ),
    )
    result = {
        "schema_version": 1,
        "kind": "harmony_policy_lab_smoke_result",
        "event_ids": [event["event_id"] for _, event in ordered],
    }
    result["semantic_sha256"] = hashlib.sha256(_canonical_json(result)).hexdigest()
    return result
