"""Per-unit admission: the queue below a saturated engine.

An engine admits `max_concurrent` requests at a time (its scheduler limit,
derived from the launch line — `--max-num-seqs` for vLLM, `--parallel` for
Strata). Requests beyond that WAIT, FIFO, bounded at 64; beyond the bound the
answer is a 429 naming the queue state, never an unbounded pile-up inside an
engine that is already at its limit and never a silent drop.

Two rules the rest of the router turns on:

* A resident unit with a queue of its own is not a shortcut: the resident-reuse
  optimisation applies only while the unit is below `max_concurrent` WITH AN
  EMPTY QUEUE (`has_capacity`). When it is saturated the ROUTER decides, and it
  may move the model or pick a sibling — that is what the planner is for.
* Waiting is measured (`queue_ms`), because a demand record that cannot say how
  long a request queued cannot show a saturated unit to anyone.

A release is idempotent: the response paths release at three different points
(stream end, upstream error, refusal replay), and a double release must not
widen the gate.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager

DEFAULT_MAX_WAITING = 64


class QueueFull(Exception):
    """The unit's waiting queue is at its bound. Carries the reason the caller
    is shown (429) — depth, bound, in-flight and the engine limit, so the
    numbers a caller can act on are in the refusal."""


class Slot:
    """One request's place in the queue. `release()` gives it back, once."""

    def __init__(self, queue: "UnitQueue", queue_ms: float):
        self._queue = queue
        self.queue_ms = queue_ms
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._queue._give_back()


class UnitQueue:
    """In-flight gate + bounded FIFO wait for one unit."""

    def __init__(self, name: str, max_concurrent: int,
                 max_waiting: int = DEFAULT_MAX_WAITING):
        self.name = name
        self.max_concurrent = max(1, int(max_concurrent))
        self.max_waiting = max(1, int(max_waiting))
        self._cv = threading.Condition()
        self._in_flight = 0
        self._waiting = 0

    def has_capacity(self) -> bool:
        """Below the engine's limit AND nobody waiting. The shortcut rule:
        only a unit with room is allowed to answer without the router."""
        with self._cv:
            return self._in_flight < self.max_concurrent and self._waiting == 0

    def acquire(self) -> Slot:
        """Take a slot, waiting FIFO while the engine is at its limit.

        Raises `QueueFull` (the caller answers 429 with its text) when the
        waiting bound is already reached."""
        started = time.monotonic()
        with self._cv:
            if self._waiting >= self.max_waiting:
                raise QueueFull(
                    f"{self.name}: queue is full — {self._waiting} waiting "
                    f"(bound {self.max_waiting}), {self._in_flight} in flight "
                    f"(max_concurrent {self.max_concurrent}); retry later")
            self._waiting += 1
            try:
                while self._in_flight >= self.max_concurrent:
                    self._cv.wait()
                self._waiting -= 1
                self._in_flight += 1
            except BaseException:
                self._waiting -= 1
                self._cv.notify()
                raise
        return Slot(self, (time.monotonic() - started) * 1000.0)

    @contextmanager
    def admit(self):
        slot = self.acquire()
        try:
            yield slot
        finally:
            slot.release()

    def _give_back(self) -> None:
        with self._cv:
            self._in_flight = max(0, self._in_flight - 1)
            self._cv.notify()

    def status(self) -> dict:
        with self._cv:
            return {"in_flight": self._in_flight, "waiting": self._waiting,
                    "max_concurrent": self.max_concurrent,
                    "max_waiting": self.max_waiting}


class Queues:
    """The per-unit queues of one node. Created on first use from the unit's
    derived `max_concurrent` — a units file can add a unit without a code
    change, and a unit nobody has asked for has no queue yet."""

    def __init__(self, max_waiting: int = DEFAULT_MAX_WAITING):
        self._max_waiting = max_waiting
        self._by_name: "dict[str, UnitQueue]" = {}
        self._lock = threading.Lock()

    def _queue(self, name: str, max_concurrent: int) -> UnitQueue:
        with self._lock:
            q = self._by_name.get(name)
            if q is None:
                q = UnitQueue(name, max_concurrent, self._max_waiting)
                self._by_name[name] = q
            return q

    def acquire(self, name: str, max_concurrent: int) -> Slot:
        return self._queue(name, max_concurrent).acquire()

    def has_capacity(self, name: str, max_concurrent: int) -> bool:
        with self._lock:
            q = self._by_name.get(name)
        # Never asked for = nobody waiting = capacity: the first request does
        # not queue behind a queue that does not exist yet.
        return q.has_capacity() if q is not None else True

    def status(self, name: str, max_concurrent: int) -> dict:
        return self._queue(name, max_concurrent).status()
