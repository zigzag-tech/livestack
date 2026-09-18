import pytest

from livestack_node.policy_lab.delegation import DelegationError, DelegationManager


def test_s14_delegated_capacity_is_bounded_across_regions_and_epochs():
    manager = DelegationManager({"gpu": 4})
    manager.issue("canada", resource_id="gpu", owner="authority", epoch=1, limit=2, expires_at_us=100)
    manager.issue("china", resource_id="gpu", owner="authority", epoch=1, limit=2, expires_at_us=100)
    manager.grant("canada", "ca-1", units=2, epoch=1, at_us=10)
    manager.grant("china", "cn-1", units=2, epoch=1, at_us=10)
    with pytest.raises(DelegationError, match="exhausted"):
        manager.grant("china", "cn-2", units=1, epoch=1, at_us=11)
    assert manager.total_active("gpu") == 4


def test_partition_expiry_and_stale_epoch_cannot_create_new_grants_but_active_lease_survives():
    manager = DelegationManager({"gpu": 2})
    manager.issue("canada", resource_id="gpu", owner="authority", epoch=5, limit=2, expires_at_us=100)
    manager.grant("canada", "active", units=1, epoch=5, at_us=50)
    with pytest.raises(DelegationError, match="expired"):
        manager.grant("canada", "late", units=1, epoch=5, at_us=101)
    manager.issue("canada", resource_id="gpu", owner="authority", epoch=6, limit=1, expires_at_us=200)
    with pytest.raises(DelegationError, match="stale epoch"):
        manager.grant("canada", "stale", units=1, epoch=5, at_us=110)
    assert manager.grant_state("active") == "active"
    manager.release("active", epoch=5)
    assert manager.grant_state("active") == "released"


def test_s15_region_permission_is_applied_before_speed_score():
    manager = DelegationManager({"gpu": 2})
    candidates = [
        {"region": "forbidden-fast", "latency_us": 1},
        {"region": "allowed-slow", "latency_us": 100},
    ]
    chosen = manager.choose_permitted(candidates, permitted_regions={"allowed-slow"})
    assert chosen["region"] == "allowed-slow"
    with pytest.raises(DelegationError, match="no permitted"):
        manager.choose_permitted(candidates, permitted_regions={"nowhere"})
