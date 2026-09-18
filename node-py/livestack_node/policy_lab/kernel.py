"""Deterministic integer-microsecond discrete-event kernel."""

from __future__ import annotations

import hashlib
import heapq
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from .contracts import ContractError


@dataclass(order=True, frozen=True)
class ScheduledEvent:
    time_us: int
    phase: int
    creation_sequence: int
    kind: str = field(compare=False)
    entity_id: str = field(compare=False)
    payload: dict[str, Any] = field(compare=False, default_factory=dict)
    generation: int | None = field(compare=False, default=None)


@dataclass(frozen=True)
class ProcessedEvent:
    time_us: int
    phase: int
    kind: str
    entity_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class KernelResult:
    processed: tuple[ProcessedEvent, ...]
    termination: str
    stale_events: int
    skipped_deadline_fences: int
    unfinished_events: int
    semantic_sha256: str


Handler = Callable[[ScheduledEvent, "EventKernel"], None]


class EventKernel:
    def __init__(
        self,
        *,
        horizon_us: int,
        drain_horizon_us: int,
        max_events: int,
        max_same_time: int,
    ) -> None:
        for name, value in (
            ("horizon_us", horizon_us),
            ("drain_horizon_us", drain_horizon_us),
            ("max_events", max_events),
            ("max_same_time", max_same_time),
        ):
            if type(value) is not int or value <= 0:
                raise ContractError(f"{name} must be a positive integer")
        if drain_horizon_us < horizon_us:
            raise ContractError("drain_horizon_us cannot precede horizon_us")
        self.horizon_us = horizon_us
        self.drain_horizon_us = drain_horizon_us
        self.max_events = max_events
        self.max_same_time = max_same_time
        self.now_us = 0
        self._sequence = 0
        self._queue: list[ScheduledEvent] = []
        self._generations: dict[str, int] = {}

    def generation(self, entity_id: str) -> int:
        return self._generations.get(entity_id, 0)

    def bump_generation(self, entity_id: str) -> int:
        value = self.generation(entity_id) + 1
        self._generations[entity_id] = value
        return value

    def schedule(
        self,
        time_us: int,
        phase: int,
        kind: str,
        entity_id: str,
        payload: Mapping[str, Any] | None = None,
        *,
        generation: int | None = None,
    ) -> None:
        if type(time_us) is not int or time_us < self.now_us:
            raise ContractError("event cannot be scheduled in the past")
        if type(phase) is not int or not 0 <= phase <= 3:
            raise ContractError("event phase must be 0..3")
        if not isinstance(kind, str) or not kind or not isinstance(entity_id, str) or not entity_id:
            raise ContractError("event kind and entity_id must be non-empty")
        event = ScheduledEvent(
            time_us,
            phase,
            self._sequence,
            kind,
            entity_id,
            dict(payload or {}),
            generation,
        )
        self._sequence += 1
        heapq.heappush(self._queue, event)

    def schedule_many(self, events: Iterable[Mapping[str, Any]]) -> None:
        normalized = sorted(
            events,
            key=lambda item: (
                item["time_us"],
                item["phase"],
                item["entity_id"],
                item["kind"],
            ),
        )
        for item in normalized:
            self.schedule(
                item["time_us"],
                item["phase"],
                item["kind"],
                item["entity_id"],
                item.get("payload"),
                generation=item.get("generation"),
            )

    def _completion_due(self, fence: ScheduledEvent) -> bool:
        return any(
            queued.time_us == fence.time_us
            and queued.entity_id == fence.entity_id
            and queued.kind == "terminal_completion"
            and (queued.generation is None or queued.generation == self.generation(queued.entity_id))
            for queued in self._queue
        )

    def run(self, handlers: Mapping[str, Handler] | None = None) -> KernelResult:
        handlers = handlers or {}
        processed: list[ProcessedEvent] = []
        stale = 0
        skipped_fences = 0
        same_time = 0
        last_time: int | None = None
        termination = "drained"
        while self._queue:
            if len(processed) >= self.max_events:
                termination = "max_events"
                break
            event = heapq.heappop(self._queue)
            if event.time_us > self.drain_horizon_us:
                heapq.heappush(self._queue, event)
                termination = "drain_horizon"
                break
            if event.time_us == last_time:
                same_time += 1
            else:
                last_time = event.time_us
                same_time = 1
            if same_time > self.max_same_time:
                heapq.heappush(self._queue, event)
                termination = "max_same_time"
                break
            self.now_us = event.time_us
            if event.generation is not None and event.generation != self.generation(event.entity_id):
                stale += 1
                continue
            if event.kind == "deadline_fence" and self._completion_due(event):
                skipped_fences += 1
                continue
            processed.append(
                ProcessedEvent(event.time_us, event.phase, event.kind, event.entity_id, event.payload)
            )
            handler = handlers.get(event.kind)
            if handler is not None:
                handler(event, self)

        semantic = [row.__dict__ for row in processed]
        digest = hashlib.sha256(
            json.dumps(
                semantic,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
        return KernelResult(
            processed=tuple(processed),
            termination=termination,
            stale_events=stale,
            skipped_deadline_fences=skipped_fences,
            unfinished_events=len(self._queue),
            semantic_sha256=digest,
        )
