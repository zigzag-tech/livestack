import asyncio
import threading
import time

import pytest

from unit_queue import QueueFull, UnitQueue, wait_budget


def test_wait_budget_validates_wire_numbers():
    assert wait_budget({"harmony_max_wait_s": 0}) == 0
    assert wait_budget({"harmony_max_wait_s": 12.5}) == 12.5
    for value in [True, -1, float("nan"), float("inf"), "10", 3601]:
        with pytest.raises(ValueError):
            wait_budget({"harmony_max_wait_s": value})


def test_estimate_refuses_an_impossible_wait_without_enqueuing():
    q = UnitQueue("flash", 1)
    held = q.acquire(service_s=10)
    try:
        assert q.status()["estimated_wait_s"] > 9
        with pytest.raises(QueueFull) as exc:
            q.acquire(service_s=2, max_wait_s=1)
        assert exc.value.detail["error"] == "wait_budget_exceeded"
        assert exc.value.detail["estimated_wait_s"] > 9
        assert q.status()["waiting"] == 0
    finally:
        held.release()


def test_deadline_expires_even_if_engine_runs_longer_than_estimated():
    q = UnitQueue("flash", 1)
    held = q.acquire(service_s=0.001)
    time.sleep(0.005)
    started = time.monotonic()
    try:
        with pytest.raises(QueueFull) as exc:
            q.acquire(service_s=1, max_wait_s=0.03)
        assert exc.value.detail["error"] == "queue_deadline_exceeded"
        assert time.monotonic() - started < 0.2
        assert q.status()["waiting"] == 0
    finally:
        held.release()


def test_fifo_waits_then_serves_and_measures_service_time():
    q = UnitQueue("flash", 1)
    held = q.acquire(service_s=0.05)
    results = []
    def run(i):
        slot = q.acquire(service_s=0.01, max_wait_s=1)
        results.append(i)
        time.sleep(0.01)
        slot.release()
    threads = []
    for i in range(3):
        t = threading.Thread(target=run, args=(i,)); t.start(); threads.append(t)
        deadline = time.monotonic() + 1
        while q.status()["waiting"] != i + 1 and time.monotonic() < deadline:
            time.sleep(0.001)
        assert q.status()["waiting"] == i + 1
    held.release()
    for t in threads: t.join(1)
    assert results == [0, 1, 2]
    assert q.status()["service_samples"] == 4
    assert q.status()["in_flight"] == 0


def test_cancelled_async_waiter_never_leaks_a_slot():
    async def run():
        q = UnitQueue("flash", 1)
        held = q.acquire(service_s=1)
        task = asyncio.create_task(q.acquire_async(service_s=1, max_wait_s=2))
        while q.status()["waiting"] == 0: await asyncio.sleep(0.001)
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
        held.release()
        assert q.status()["waiting"] == 0
        assert q.status()["in_flight"] == 0
    asyncio.run(run())
