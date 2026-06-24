"""Lease-store conformance vectors (port 1). Deterministic via an injected clock."""

from livestack_node.lease import (
    Capability,
    CapabilityLeaseStore,
    Requirement,
    matches_requirement,
)


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_store(clock=None):
    clock = clock or FakeClock()
    store = CapabilityLeaseStore(clock=clock)
    store.register(Capability(kind="asr", host_id="h", slots=1, lease_ttl_seconds=100))
    store.register(Capability(kind="diarize", host_id="h", slots=1, lease_ttl_seconds=100))
    return store, clock


def test_acquire_sets_expiry_from_ttl():
    store, clock = make_store()
    lease = store.acquire_lease(Requirement(kind="asr"), owner_id="o")
    assert lease is not None
    assert lease.capability_kind == "asr"
    assert lease.expires_at == clock.t + 100


def test_explicit_ttl_overrides_capability_default():
    store, clock = make_store()
    lease = store.acquire_lease(Requirement(kind="asr"), owner_id="o", ttl_seconds=5)
    assert lease.expires_at == clock.t + 5


def test_slots_exhausted_returns_none():
    store, clock = make_store()
    assert store.acquire_lease(Requirement(kind="asr"), owner_id="a") is not None
    assert store.acquire_lease(Requirement(kind="asr"), owner_id="b") is None  # slots=1


def test_lease_expires_after_ttl_without_heartbeat():
    store, clock = make_store()
    store.acquire_lease(Requirement(kind="asr"), owner_id="o")
    assert store.active_kinds() == {"asr"}
    clock.t += 101
    assert store.active_kinds() == set()
    assert store.reap_expired() != []


def test_heartbeat_extends_expiry():
    store, clock = make_store()
    lease = store.acquire_lease(Requirement(kind="asr"), owner_id="o")
    clock.t += 99
    store.heartbeat_lease(lease.lease_id)
    assert store.active_kinds() == {"asr"}
    clock.t += 99  # 198 since acquire, but only 99 since heartbeat
    assert store.active_kinds() == {"asr"}


def test_release_frees_slot():
    store, clock = make_store()
    lease = store.acquire_lease(Requirement(kind="asr"), owner_id="a")
    assert store.acquire_lease(Requirement(kind="asr"), owner_id="b") is None
    assert store.release_lease(lease.lease_id) is True
    assert store.acquire_lease(Requirement(kind="asr"), owner_id="b") is not None


def test_per_kind_isolation():
    store, clock = make_store()
    store.acquire_lease(Requirement(kind="asr"), owner_id="o")
    assert store.active_kinds() == {"asr"}
    assert "diarize" not in store.active_kinds()


def test_matches_requirement_labels_and_hosts():
    cap = Capability(kind="asr", host_id="h", labels={"device": "cuda:0"})
    assert matches_requirement(cap, Requirement(kind="asr"))
    assert matches_requirement(cap, Requirement(kind="asr", labels={"device": "cuda:0"}))
    assert not matches_requirement(cap, Requirement(kind="asr", labels={"device": "cpu"}))
    assert not matches_requirement(cap, Requirement(kind="asr", host_ids=["other"]))
    assert matches_requirement(cap, Requirement(kind="asr", host_ids=["h"]))
