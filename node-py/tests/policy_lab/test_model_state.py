import pytest

from livestack_node.policy_lab.model_state import ModelStateError, ReplicaManager
from livestack_node.policy_lab.resources import PhysicalResourceCatalog, ReservationLedger


def _manager(capacity=100):
    catalog = PhysicalResourceCatalog()
    catalog.add("gpu", {"gpu_bytes": capacity})
    return ReplicaManager(ReservationLedger(catalog))


def test_s07_concurrent_cold_requests_coalesce_one_preparation():
    manager = _manager()
    manager.define_replica(
        "model", "gpu", weights={"gpu_bytes": 40}, staging={"gpu_bytes": 20}, loadable=True
    )
    assert manager.request_prepare("model", "request-a", epoch=1, fence=1) == "started"
    assert manager.request_prepare("model", "request-b", epoch=1, fence=1) == "coalesced"
    assert manager.replica("model").state == "loading"
    assert manager.replica("model").waiting_requests == {"request-a", "request-b"}
    assert manager.load_count == 1
    manager.finish_prepare("model", success=True, epoch=1, fence=1)
    assert manager.replica("model").state == "resident"
    assert manager.ledger.used("gpu") == {"gpu_bytes": 40}


def test_s16_failed_load_never_serves_and_retains_measured_residual():
    manager = _manager()
    manager.define_replica(
        "model", "gpu", weights={"gpu_bytes": 40}, staging={"gpu_bytes": 20}, loadable=True
    )
    manager.request_prepare("model", "request", epoch=2, fence=3)
    manager.finish_prepare(
        "model", success=False, epoch=2, fence=3, residual={"gpu_bytes": 7}
    )
    replica = manager.replica("model")
    assert replica.state == "absent"
    assert not manager.can_serve("model")
    assert replica.residual_lease_id is not None
    assert manager.ledger.used("gpu") == {"gpu_bytes": 7}


def test_missing_weights_are_not_magically_loadable():
    manager = _manager()
    manager.define_replica(
        "model", "gpu", weights={"gpu_bytes": 40}, staging={"gpu_bytes": 20}, loadable=False
    )
    with pytest.raises(ModelStateError, match="not loadable"):
        manager.request_prepare("model", "request", epoch=1, fence=1)


def test_s29_active_stream_cannot_be_evicted():
    manager = _manager()
    manager.define_replica(
        "model", "gpu", weights={"gpu_bytes": 40}, staging={"gpu_bytes": 20}, loadable=True
    )
    manager.request_prepare("model", "request", epoch=1, fence=1)
    manager.finish_prepare("model", success=True, epoch=1, fence=1)
    manager.start_request("model")
    with pytest.raises(ModelStateError, match="active requests"):
        manager.evict_idle("model", epoch=1, fence=1)
    manager.finish_request("model")
    manager.evict_idle("model", epoch=1, fence=1)
    assert manager.replica("model").state == "absent"
    assert manager.ledger.used("gpu") == {"gpu_bytes": 0}
