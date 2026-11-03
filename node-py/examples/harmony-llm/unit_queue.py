"""Bounded FIFO admission with estimated waits and caller-owned deadlines.

Estimates are approximate: active work retains its predicted completion time;
queued work is placed on the earliest available batch slot. Actual elapsed
service times update an EWMA, used explicitly as a bootstrap when no token-rate
measurement exists. A deadline is enforced even if that estimate proves wrong.
"""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager
import heapq
import math
import threading
import time

DEFAULT_MAX_WAITING = 64
DEFAULT_MAX_WAIT_S = 300.0


def wait_budget(body: dict | None) -> float:
    value = (body or {}).get("harmony_max_wait_s", DEFAULT_MAX_WAIT_S)
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not math.isfinite(value) or not 0 <= value <= 3600):
        raise ValueError("harmony_max_wait_s must be a finite number from 0 to 3600")
    return float(value)


class QueueFull(Exception):
    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.detail = detail or {"error": "queue_full", "message": message}


class Slot:
    def __init__(self, queue: "UnitQueue", queue_ms: float, service_s: float = 60.0):
        self._queue = queue
        self.queue_ms = queue_ms
        self.started = time.monotonic()
        self.expected_end = self.started + service_s
        self._released = False

    def release(self) -> None:
        with self._queue._cv:
            if self._released:
                return
            self._released = True
            self._queue._give_back(self)


class UnitQueue:
    def __init__(self, name: str, max_concurrent: int,
                 max_waiting: int = DEFAULT_MAX_WAITING):
        self.name = name
        self.max_concurrent = max(1, int(max_concurrent))
        self.max_waiting = max(1, int(max_waiting))
        self._cv = threading.Condition()
        self._active: list[Slot] = []
        self._tickets: deque = deque()
        self._service_s = 60.0
        self._samples = 0

    def _estimate(self, now: float) -> float:
        lanes = [max(now, s.expected_end) for s in self._active]
        lanes += [now] * (self.max_concurrent - len(lanes))
        heapq.heapify(lanes)
        for _, duration in self._tickets:
            heapq.heappush(lanes, heapq.heappop(lanes) + duration)
        return max(0.0, lanes[0] - now)

    def has_capacity(self) -> bool:
        with self._cv:
            return len(self._active) < self.max_concurrent and not self._tickets

    def _slot(self, started: float, duration: float) -> Slot:
        slot = Slot(self, (time.monotonic() - started) * 1000, duration)
        self._active.append(slot)
        return slot

    def acquire(self, service_s: float | None = None,
                max_wait_s: float = DEFAULT_MAX_WAIT_S,
                cancel: threading.Event | None = None) -> Slot:
        started = time.monotonic()
        deadline = started + max_wait_s
        with self._cv:
            duration = self._service_s if service_s is None else max(0.001, service_s)
            estimate = self._estimate(started)
            detail = {"unit": self.name, "estimated_wait_s": round(estimate, 3),
                      "estimated_service_s": round(duration, 3), "max_wait_s": max_wait_s,
                      "approximate": True}
            if cancel is not None and cancel.is_set():
                raise asyncio.CancelledError()
            if not self._tickets and len(self._active) < self.max_concurrent:
                return self._slot(started, duration)
            if len(self._tickets) >= self.max_waiting:
                raise QueueFull(f"{self.name}: queue is full ({len(self._tickets)} waiting, "
                                f"max_concurrent {self.max_concurrent})", {**detail, "error": "queue_full"})
            if estimate > max_wait_s or max_wait_s == 0:
                raise QueueFull(f"{self.name}: estimated wait exceeds caller budget",
                                {**detail, "error": "wait_budget_exceeded"})
            ticket = (object(), duration)
            self._tickets.append(ticket)
            try:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise asyncio.CancelledError()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise QueueFull(f"{self.name}: queue deadline exceeded",
                                        {**detail, "error": "queue_deadline_exceeded"})
                    if self._tickets[0] is ticket and len(self._active) < self.max_concurrent:
                        self._tickets.popleft()
                        self._cv.notify_all()
                        return self._slot(started, duration)
                    self._cv.wait(min(remaining, 0.1))
            finally:
                if ticket in self._tickets:
                    self._tickets.remove(ticket)
                    self._cv.notify_all()

    async def acquire_async(self, **kwargs) -> Slot:
        cancel = threading.Event()
        task = asyncio.create_task(asyncio.to_thread(self.acquire, cancel=cancel, **kwargs))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            cancel.set()
            with self._cv:
                self._cv.notify_all()
            try:
                slot = await task
            except (asyncio.CancelledError, QueueFull):
                pass
            else:
                slot.release()
            raise

    def try_acquire(self) -> Slot | None:
        with self._cv:
            if self._tickets or len(self._active) >= self.max_concurrent:
                return None
            return self._slot(time.monotonic(), self._service_s)

    @contextmanager
    def admit(self):
        slot = self.acquire()
        try:
            yield slot
        finally:
            slot.release()

    def _give_back(self, slot: Slot) -> None:
        self._active.remove(slot)
        elapsed = max(0.001, time.monotonic() - slot.started)
        self._service_s = elapsed if not self._samples else 0.8 * self._service_s + 0.2 * elapsed
        self._samples += 1
        self._cv.notify_all()

    def status(self) -> dict:
        with self._cv:
            return {"in_flight": len(self._active), "waiting": len(self._tickets),
                    "max_concurrent": self.max_concurrent, "max_waiting": self.max_waiting,
                    "estimated_wait_s": round(self._estimate(time.monotonic()), 3),
                    "estimated_service_s": round(self._service_s, 3),
                    "service_samples": self._samples,
                    "estimate_source": "elapsed_service_ewma" if self._samples else "bootstrap_60s",
                    "approximate": True, "default_max_wait_s": DEFAULT_MAX_WAIT_S}


class Queues:
    def __init__(self, max_waiting: int = DEFAULT_MAX_WAITING):
        self._max_waiting = max_waiting
        self._by_name: dict[str, UnitQueue] = {}
        self._lock = threading.Lock()

    def _queue(self, name: str, max_concurrent: int) -> UnitQueue:
        with self._lock:
            if name not in self._by_name:
                self._by_name[name] = UnitQueue(name, max_concurrent, self._max_waiting)
            return self._by_name[name]

    def acquire(self, name: str, max_concurrent: int, **kwargs) -> Slot:
        return self._queue(name, max_concurrent).acquire(**kwargs)

    async def acquire_async(self, name: str, max_concurrent: int, **kwargs) -> Slot:
        return await self._queue(name, max_concurrent).acquire_async(**kwargs)

    def try_acquire(self, name: str, max_concurrent: int) -> Slot | None:
        return self._queue(name, max_concurrent).try_acquire()

    def has_capacity(self, name: str, max_concurrent: int) -> bool:
        return self._queue(name, max_concurrent).has_capacity()

    def status(self, name: str, max_concurrent: int) -> dict:
        return self._queue(name, max_concurrent).status()
