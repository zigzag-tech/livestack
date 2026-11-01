from concurrent.futures import ThreadPoolExecutor

import pytest

from livestack_node.device_leases import (
    MAX_TTL_S,
    MAX_WAIT_TIMEOUT_S,
    DeviceLeaseStore,
)


@pytest.fixture
def store(tmp_path):
    result = DeviceLeaseStore(str(tmp_path / "device-leases.sqlite"))
    yield result
    result.close()


def begin(store, request_id="request-1", device_id="gpu-0", owner="owner-1", **overrides):
    arguments = {
        "min_free_bytes": 1024,
        "wait_timeout_s": 10,
        "ttl_s": 30,
        "now": 100,
    }
    arguments.update(overrides)
    return store.begin(request_id, device_id, owner, **arguments)


def test_begin_validates_identifiers_min_free_bytes_and_time_bounds(store):
    for field, value in (
        ("request_id", ""),
        ("device_id", " "),
        ("owner", None),
    ):
        arguments = {"request_id": "r", "device_id": "d", "owner": "o"}
        arguments[field] = value
        with pytest.raises(ValueError):
            begin(store, **arguments)

    for value in (0, -1, 1.5, True):
        with pytest.raises(ValueError):
            begin(store, request_id=f"min-{value}", min_free_bytes=value)
    for field, maximum in (("wait_timeout_s", MAX_WAIT_TIMEOUT_S), ("ttl_s", MAX_TTL_S)):
        for value in (0, -1, maximum + 1, True, float("nan"), float("inf")):
            with pytest.raises(ValueError):
                begin(store, request_id=f"{field}-{value}", **{field: value})
    for value in (True, "100", float("nan"), float("inf")):
        with pytest.raises(ValueError):
            begin(store, request_id=f"now-{value}", now=value)


def test_mark_exclusive_validates_free_bytes_request_and_lease(store):
    record = begin(store)
    for value in (-1, 1.5, True):
        with pytest.raises(ValueError):
            store.mark_exclusive(record["request_id"], record["lease_id"], value, 101)
    with pytest.raises(ValueError):
        store.mark_exclusive("missing", record["lease_id"], 2048, 101)
    with pytest.raises(ValueError):
        store.mark_exclusive(record["request_id"], "wrong-lease", 2048, 101)
    assert store.get(record["request_id"])["state"] == "draining"


def test_one_active_request_per_device(store):
    begin(store, request_id="first")
    with pytest.raises(RuntimeError):
        begin(store, request_id="second", owner="other")
    assert [r["request_id"] for r in store.active("gpu-0")] == ["first"]


def test_begin_is_idempotent_for_same_owner_and_parameters(store):
    first = begin(store)
    second = begin(store, now=999)
    assert second == first
    assert len(store.active()) == 1


def test_begin_rejects_owner_mismatch_and_changed_parameters(store):
    begin(store)
    with pytest.raises(ValueError):
        begin(store, owner="other")
    changes = (
        {"device_id": "gpu-1"},
        {"min_free_bytes": 2048},
        {"wait_timeout_s": 20},
        {"ttl_s": 60},
    )
    for change in changes:
        with pytest.raises(ValueError):
            begin(store, **change)


def test_mark_exclusive_records_free_bytes_and_is_idempotent(store):
    record = begin(store)
    exclusive = store.mark_exclusive(record["request_id"], record["lease_id"], 4096, 105)
    assert exclusive["state"] == "exclusive"
    assert exclusive["min_free_bytes"] == 1024
    assert exclusive["free_bytes"] == 4096
    assert exclusive["updated_at"] == 105
    assert exclusive["expires_at"] == 135
    repeated = store.mark_exclusive(record["request_id"], record["lease_id"], 8192, 106)
    assert repeated == exclusive


def test_draining_request_cannot_become_exclusive_after_wait_timeout(store):
    record = begin(store, wait_timeout_s=5)
    with pytest.raises(RuntimeError):
        store.mark_exclusive(record["request_id"], record["lease_id"], 2048, 105)
    assert store.get(record["request_id"])["state"] == "draining"


def test_heartbeat_extends_live_exclusive_lease(store):
    record = begin(store)
    store.mark_exclusive(record["request_id"], record["lease_id"], 2048, 100)
    assert store.heartbeat(record["lease_id"], 50, 120) is True
    renewed = store.get_lease(record["lease_id"])
    assert renewed["expires_at"] == 170
    assert renewed["updated_at"] == 120
    assert store.get_lease(record["lease_id"])["free_bytes"] == 2048


def test_heartbeat_unknown_nonexclusive_and_expired_returns_false(store):
    record = begin(store)
    assert store.heartbeat("missing", 10, 100) is False
    assert store.heartbeat(record["lease_id"], 10, 100) is False
    store.mark_exclusive(record["request_id"], record["lease_id"], 2048, 100)
    assert store.heartbeat(record["lease_id"], 10, 130) is False
    assert store.get(record["request_id"])["state"] == "exclusive"


def test_release_is_idempotent_and_frees_device(store):
    record = begin(store)
    assert store.release("missing", 101) is False
    assert store.release(record["lease_id"], 101) is True
    assert store.get(record["request_id"])["state"] == "released"
    assert store.release(record["lease_id"], 102) is True
    assert store.state("gpu-0")["state"] == "shared"
    replacement = begin(store, request_id="replacement", owner="other", now=103)
    assert replacement["state"] == "draining"


def test_expire_moves_draining_and_exclusive_to_expired_draining(store):
    draining = begin(store, request_id="draining", device_id="gpu-0", wait_timeout_s=5)
    exclusive = begin(store, request_id="exclusive", device_id="gpu-1", ttl_s=20)
    store.mark_exclusive(exclusive["request_id"], exclusive["lease_id"], 2048, 100)
    live = begin(store, request_id="live", device_id="gpu-2", wait_timeout_s=50)
    assert store.expire(104) == []
    expired = store.expire(105)
    assert [r["request_id"] for r in expired] == ["draining"]
    assert store.get(draining["request_id"])["state"] == "expired_draining"
    assert store.get(exclusive["request_id"])["state"] == "exclusive"
    expired = store.expire(120)
    assert [r["request_id"] for r in expired] == ["exclusive"]
    assert all(r["state"] == "expired_draining" for r in expired)
    assert store.get(live["request_id"])["state"] == "draining"
    assert store.state("gpu-0")["state"] == "expired_draining"
    assert store.state("gpu-1")["state"] == "expired_draining"
    assert store.expire(120) == []


def test_expired_draining_blocks_until_release(store):
    record = begin(store, wait_timeout_s=5)
    store.expire(105)
    with pytest.raises(RuntimeError):
        begin(store, request_id="blocked")
    assert store.heartbeat(record["lease_id"], 30, 106) is False
    assert store.release(record["lease_id"], 107) is True
    assert store.get(record["request_id"])["state"] == "released"
    assert begin(store, request_id="next", now=108)["state"] == "draining"


def test_fail_and_timeout_are_terminal_and_free_device(store):
    failed = begin(store, request_id="failed", device_id="gpu-0")
    failed = store.fail(failed["request_id"], "drain failed", 101)
    assert failed["state"] == "failed"
    assert failed["reason"] == "drain failed"
    assert store.state("gpu-0")["state"] == "shared"

    timeout = begin(store, request_id="timeout", device_id="gpu-0", now=102)
    timeout = store.fail(timeout["request_id"], "wait elapsed", 103, terminal_state="timeout")
    assert timeout["state"] == "timeout"
    assert timeout["reason"] == "wait elapsed"
    assert store.active() == []
    with pytest.raises(RuntimeError):
        store.fail(timeout["request_id"], "again", 104)


def test_fail_validates_reason_terminal_state_and_request(store):
    record = begin(store)
    with pytest.raises(ValueError):
        store.fail("missing", "reason", 100)
    with pytest.raises(ValueError):
        store.fail(record["request_id"], "", 100)
    with pytest.raises(ValueError):
        store.fail(record["request_id"], "reason", 100, terminal_state="released")
    assert store.get(record["request_id"])["state"] == "draining"


def test_active_leases_and_observations_survive_reopen(tmp_path):
    path = str(tmp_path / "durable.sqlite")
    first = DeviceLeaseStore(path)
    record = begin(first)
    first.mark_exclusive(record["request_id"], record["lease_id"], 8192, 101)
    first.heartbeat(record["lease_id"], 60, 102)
    first.close()

    reopened = DeviceLeaseStore(path)
    try:
        restored = reopened.get_lease(record["lease_id"])
        assert restored["state"] == "exclusive"
        assert restored["min_free_bytes"] == 1024
        assert restored["free_bytes"] == 8192
        assert restored["expires_at"] == 162
        assert reopened.active("gpu-0")[0]["request_id"] == record["request_id"]
        with pytest.raises(RuntimeError):
            begin(reopened, request_id="other")
        assert reopened.release(record["lease_id"], 103) is True
    finally:
        reopened.close()

    terminal = DeviceLeaseStore(path)
    try:
        assert terminal.get(record["request_id"])["state"] == "released"
        assert terminal.state("gpu-0")["state"] == "shared"
    finally:
        terminal.close()


def test_state_defaults_to_shared_and_projects_active_lease(store):
    assert store.state("gpu-0") == {
        "state": "shared",
        "owner": None,
        "lease_id": None,
        "blockers": [],
    }
    record = begin(store)
    blockers = [{"kind": "free_bytes", "required": 1024, "available": 512}]
    store.blockers(record["request_id"], blockers, 101)
    assert store.state("gpu-0") == {
        "state": "draining",
        "owner": "owner-1",
        "lease_id": record["lease_id"],
        "blockers": blockers,
    }


def test_blockers_persist_without_terminal_failure(store):
    record = begin(store)
    blockers = [{"code": "workers", "request_ids": ["a", "b"]}]
    updated = store.blockers(record["request_id"], blockers, 101)
    assert updated["state"] == "draining"
    assert updated["blockers"] == blockers
    assert updated["updated_at"] == 101
    blockers.append({"code": "client-mutation"})
    assert store.get(record["request_id"])["blockers"] != blockers
    with pytest.raises(ValueError):
        store.blockers("missing", [], 102)
    with pytest.raises(ValueError):
        store.blockers(record["request_id"], "not structured", 102)
    with pytest.raises(ValueError):
        store.blockers(record["request_id"], [{"bad": float("nan")}], 102)


def test_multiple_devices_are_independent_and_active_can_be_filtered(store):
    first = begin(store, request_id="first", device_id="gpu-0")
    second = begin(store, request_id="second", device_id="gpu-1", owner="owner-2")
    store.mark_exclusive(first["request_id"], first["lease_id"], 2048, 101)
    store.release(first["lease_id"], 102)
    assert store.active("gpu-0") == []
    assert [r["request_id"] for r in store.active("gpu-1")] == ["second"]
    assert [r["request_id"] for r in store.active()] == ["second"]
    assert store.state("gpu-0")["state"] == "shared"
    assert store.state("gpu-1")["owner"] == "owner-2"


def test_failed_or_released_leases_do_not_heartbeat_or_release_again(store):
    failed = begin(store, request_id="failed", device_id="gpu-0")
    store.fail(failed["request_id"], "failed", 101)
    assert store.heartbeat(failed["lease_id"], 30, 102) is False
    assert store.release(failed["lease_id"], 102) is False
    assert store.get(failed["request_id"])["state"] == "failed"

    released = begin(store, request_id="released", device_id="gpu-1")
    store.release(released["lease_id"], 101)
    assert store.heartbeat(released["lease_id"], 30, 102) is False
    assert store.release(released["lease_id"], 102) is True
    assert store.get(released["request_id"])["state"] == "released"


def test_concurrent_begin_has_one_winner_per_device(store):
    def contender(index):
        try:
            return begin(store, request_id=f"request-{index}", owner=f"owner-{index}")
        except RuntimeError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(contender, range(8)))
    records = [result for result in results if isinstance(result, dict)]
    conflicts = [result for result in results if isinstance(result, RuntimeError)]
    assert len(records) == 1
    assert len(conflicts) == 7
    assert len(store.active("gpu-0")) == 1
