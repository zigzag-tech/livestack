"""The signal that was missing during the 2026-08-04 dictation outage.

A TTS node reported every unit `resident: false` — Harmony had evicted them —
while its process still held 14.7 GB in PyTorch's caching allocator. Declared
state said "nothing resident", the driver said "full", and the planner had no
lever: you cannot evict what is already evicted. ASR on the same card then failed
every request with `CUDA out of memory. Tried to allocate 2.00 MiB`, which
surfaced to the user as empty transcripts and a 500 on batch recovery.

Nothing in the system could state that condition. These tests pin the statement.
"""
from livestack_node.meters import leak_signal

GB = 1_000_000_000


def test_an_evicted_model_that_never_returned_its_pool_is_named():
    # The outage, in numbers: nothing resident, 14.7 GB still held.
    sig = leak_signal(
        {"allocated_bytes": 0, "reserved_bytes": 14_772 * 1_000_000,
         "reclaimable_bytes": 14_772 * 1_000_000},
        resident_footprint_bytes=0,
    )
    assert sig is not None, "a node holding 14.7 GB with nothing resident must say so"
    assert sig["unexplained_bytes"] > 14 * GB
    assert sig["reclaimable_bytes"] > 14 * GB, "and that it is reclaimable"


def test_normal_overhead_is_not_a_leak():
    """Kernels, activation and fragmentation cost real memory no footprint
    declares. A signal that fires on healthy nodes is one people learn to
    ignore, which is worse than none."""
    assert leak_signal(
        {"allocated_bytes": 5 * GB, "reserved_bytes": 6 * GB, "reclaimable_bytes": 1 * GB},
        resident_footprint_bytes=5 * GB,
    ) is None


def test_a_resident_model_explains_its_own_memory():
    assert leak_signal(
        {"allocated_bytes": 14 * GB, "reserved_bytes": 15 * GB, "reclaimable_bytes": 1 * GB},
        resident_footprint_bytes=14 * GB,
    ) is None


def test_an_unavailable_reading_is_silent_rather_than_alarming():
    """A node with no CUDA, or a meter that threw, must not read as a leak."""
    assert leak_signal(None, resident_footprint_bytes=0) is None
    assert leak_signal({}, resident_footprint_bytes=0) is None


def test_the_slack_is_tunable_for_tighter_cards():
    usage = {"allocated_bytes": 0, "reserved_bytes": 1 * GB, "reclaimable_bytes": 1 * GB}
    assert leak_signal(usage, 0) is None                      # default 1.5 GB slack absorbs it
    assert leak_signal(usage, 0, slack_bytes=500_000_000) is not None
