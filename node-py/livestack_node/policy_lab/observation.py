"""Append-only observation identity, deduplication and deterministic joining."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .contracts import ContractError, StrictObjectSpec


EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "event_type",
        "request_id",
        "attempt_id",
        "decision_id",
        "parent_request_id",
        "workflow_id",
        "emitter_id",
        "emitter_boot_id",
        "sequence",
        "wall_time_utc_us",
        "monotonic_time_us",
        "clock_uncertainty_us",
        "observed_at_utc_us",
        "payload",
    }
)
EVENT_SPEC = StrictObjectSpec(required=EVENT_FIELDS)


class EventConflict(ContractError):
    """An observation identity was reused for different facts."""


def _required_id(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > 1024:
        raise ContractError(f"{name} must be a bounded non-empty string")
    return value


def _optional_id(value: Any, name: str) -> str | None:
    if value is None:
        return None
    return _required_id(value, name)


def _nonnegative_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ContractError(f"{name} must be a nonnegative integer")
    return value


@dataclass(frozen=True)
class ObservationEvent:
    schema_version: int
    event_id: str
    event_type: str
    request_id: str | None
    attempt_id: str | None
    decision_id: str | None
    parent_request_id: str | None
    workflow_id: str | None
    emitter_id: str
    emitter_boot_id: str
    sequence: int
    wall_time_utc_us: int
    monotonic_time_us: int
    clock_uncertainty_us: int
    observed_at_utc_us: int
    payload: dict[str, Any]

    @classmethod
    def from_dict(cls, value: Any) -> "ObservationEvent":
        item = EVENT_SPEC.validate(value, path="observation event")
        event_type = _required_id(item["event_type"], "event_type")
        request_id = _optional_id(item["request_id"], "request_id")
        if event_type == "request_arrived" and request_id is None:
            raise ContractError("request_arrived requires request_id")
        if not isinstance(item["payload"], dict):
            raise ContractError("payload must be an object")
        return cls(
            schema_version=1,
            event_id=_required_id(item["event_id"], "event_id"),
            event_type=event_type,
            request_id=request_id,
            attempt_id=_optional_id(item["attempt_id"], "attempt_id"),
            decision_id=_optional_id(item["decision_id"], "decision_id"),
            parent_request_id=_optional_id(item["parent_request_id"], "parent_request_id"),
            workflow_id=_optional_id(item["workflow_id"], "workflow_id"),
            emitter_id=_required_id(item["emitter_id"], "emitter_id"),
            emitter_boot_id=_required_id(item["emitter_boot_id"], "emitter_boot_id"),
            sequence=_nonnegative_int(item["sequence"], "sequence"),
            wall_time_utc_us=_nonnegative_int(item["wall_time_utc_us"], "wall_time_utc_us"),
            monotonic_time_us=_nonnegative_int(item["monotonic_time_us"], "monotonic_time_us"),
            clock_uncertainty_us=_nonnegative_int(item["clock_uncertainty_us"], "clock_uncertainty_us"),
            observed_at_utc_us=_nonnegative_int(item["observed_at_utc_us"], "observed_at_utc_us"),
            payload=item["payload"],
        )

    def canonical_bytes(self) -> bytes:
        return json.dumps(
            self.__dict__,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")


@dataclass(frozen=True)
class JoinedRequest:
    request_id: str
    events: tuple[ObservationEvent, ...]
    attempts: tuple[str, ...]


class ObservationJournal:
    """In-memory reference joiner used by importer and contract tests."""

    def __init__(self) -> None:
        self._events: dict[str, ObservationEvent] = {}
        self._event_bytes: dict[str, bytes] = {}
        self._sequence_ids: dict[tuple[str, str, int], str] = {}
        self._attempt_requests: dict[str, str] = {}

    def append(self, value: Any) -> str:
        event = ObservationEvent.from_dict(value)
        encoded = event.canonical_bytes()
        previous = self._event_bytes.get(event.event_id)
        if previous is not None:
            if previous != encoded:
                raise EventConflict(f"same event_id has conflicting content: {event.event_id}")
            return "duplicate"

        sequence_key = (event.emitter_id, event.emitter_boot_id, event.sequence)
        prior_event_id = self._sequence_ids.get(sequence_key)
        if prior_event_id is not None:
            raise EventConflict(
                "emitter boot sequence reused by different event: "
                f"{sequence_key!r} ({prior_event_id}, {event.event_id})"
            )
        if event.attempt_id is not None and event.request_id is not None:
            prior_request = self._attempt_requests.get(event.attempt_id)
            if prior_request is not None and prior_request != event.request_id:
                raise EventConflict(
                    f"attempt_id {event.attempt_id} belongs to both "
                    f"{prior_request} and {event.request_id}"
                )
            self._attempt_requests[event.attempt_id] = event.request_id

        self._events[event.event_id] = event
        self._event_bytes[event.event_id] = encoded
        self._sequence_ids[sequence_key] = event.event_id
        return "appended"

    @property
    def logical_demand_count(self) -> int:
        return len(
            {
                event.request_id
                for event in self._events.values()
                if event.event_type == "request_arrived" and event.request_id is not None
            }
        )

    def joined_requests(self) -> dict[str, JoinedRequest]:
        grouped: dict[str, list[ObservationEvent]] = {}
        for event in self._events.values():
            if event.request_id is not None:
                grouped.setdefault(event.request_id, []).append(event)
        result: dict[str, JoinedRequest] = {}
        for request_id in sorted(grouped):
            events = tuple(
                sorted(
                    grouped[request_id],
                    key=lambda event: (
                        event.wall_time_utc_us,
                        event.emitter_id,
                        event.emitter_boot_id,
                        event.sequence,
                        event.event_id,
                    ),
                )
            )
            attempts = tuple(sorted({event.attempt_id for event in events if event.attempt_id}))
            result[request_id] = JoinedRequest(request_id, events, attempts)
        return result
