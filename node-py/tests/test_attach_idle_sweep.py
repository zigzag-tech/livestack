"""attach() drives idle eviction itself — a real attach(), real manager, real clock.

On 2026-10-01 klein-0 (built with attach(idle_seconds=900)) stayed resident 48+
min after its last job: nothing called manager.maybe_evict(). These tests fail on
an attach() that does not start the sweep.
"""
import threading
import time

import pytest

fastapi = pytest.importorskip("fastapi")

from livestack_node import ManagedUnit, attach, counting, noop_free  # noqa: E402


@pytest.fixture
def node(monkeypatch):
    monkeypatch.setenv("LIVESTACK_REGISTER", "0")
    monkeypatch.setenv("LIVESTACK_ACT_SAMPLE_S", "0")
    for var in ("LIVESTACK_MESH_ENABLED", "LIVESTACK_NODE_PORT"):
        monkeypatch.delenv(var, raising=False)
    gpu_lock = threading.Lock()
    calls = []

    def gpu_call(fn):
        with gpu_lock:
            calls.append(threading.current_thread().name)
            return fn()

    busy = counting()
    unit = ManagedUnit("u", loader=lambda: "model", freer=noop_free)
    manager, coordinator = attach(fastapi.FastAPI(), host_id="h", kind="test", units={"u": unit},
                                  idle_seconds=1, coload=True, gpu_call=gpu_call,
                                  device_meter=None, in_flight=busy)
    return manager, coordinator, busy, calls


def resident(manager):
    return manager.status()["resident"]


def wait_for(predicate, seconds=5.0):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def test_an_idle_unit_is_evicted_by_attach_itself(node):
    manager, _, _, calls = node
    manager.ensure("u")
    assert resident(manager) == ["u"]
    assert wait_for(lambda: resident(manager) == []), "idle unit was never evicted"
    assert "livestack-idle-sweep" in calls, "eviction must run through gpu_call"


def test_a_unit_stays_while_the_server_counts_work(node):
    manager, _, busy, _ = node
    manager.ensure("u")
    with busy:
        time.sleep(2.5)  # > idle_seconds plus several sweep intervals
        assert resident(manager) == ["u"]
    assert wait_for(lambda: resident(manager) == [])


def test_a_unit_stays_while_leased(node):
    manager, coordinator, _, _ = node
    manager.ensure("u")
    lease = coordinator.acquire_lease("u", "consumer", ttl_seconds=60)
    time.sleep(2.5)
    assert resident(manager) == ["u"]
    coordinator.release_lease(getattr(lease, "lease_id", None) or lease["lease_id"])
    assert wait_for(lambda: resident(manager) == [])
