import pytest

from livestack_node.policy_lab.resources import (
    CapacityError,
    FenceError,
    PhysicalResourceCatalog,
    ReservationLedger,
)


def test_s24_aliases_do_not_duplicate_physical_capacity():
    catalog = PhysicalResourceCatalog()
    catalog.add("gpu-physical", {"gpu_bytes": 100}, aliases=["hostd/gpu0", "engine/cuda0"])
    ledger = ReservationLedger(catalog)
    ledger.acquire("a", "hostd/gpu0", {"gpu_bytes": 60}, epoch=1, fence=1)
    with pytest.raises(CapacityError):
        ledger.acquire("b", "engine/cuda0", {"gpu_bytes": 50}, epoch=1, fence=1)
    assert ledger.available("gpu-physical") == {"gpu_bytes": 40}


def test_s07_shared_weight_reservation_and_per_request_memory_are_separate():
    catalog = PhysicalResourceCatalog()
    catalog.add("gpu", {"gpu_bytes": 100})
    ledger = ReservationLedger(catalog)
    ledger.acquire("model:model-a", "gpu", {"gpu_bytes": 40}, epoch=1, fence=1)
    ledger.acquire("request:a", "gpu", {"gpu_bytes": 10}, epoch=1, fence=1)
    ledger.acquire("request:b", "gpu", {"gpu_bytes": 10}, epoch=1, fence=1)
    assert ledger.used("gpu") == {"gpu_bytes": 60}


def test_s18_client_timeout_does_not_release_and_cleanup_requires_current_fence():
    catalog = PhysicalResourceCatalog()
    catalog.add("gpu", {"gpu_bytes": 100})
    ledger = ReservationLedger(catalog)
    lease = ledger.acquire("request:a", "gpu", {"gpu_bytes": 80}, epoch=4, fence=9)
    ledger.note_client_timeout(lease)
    assert ledger.used("gpu") == {"gpu_bytes": 80}
    with pytest.raises(FenceError):
        ledger.release_after_cleanup(lease, epoch=4, fence=8)
    ledger.release_after_cleanup(lease, epoch=4, fence=9)
    assert ledger.used("gpu") == {"gpu_bytes": 0}
    with pytest.raises(FenceError, match="already released"):
        ledger.release_after_cleanup(lease, epoch=4, fence=9)
