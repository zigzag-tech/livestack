"""Residence-controller conformance vectors (port 3): the single rule
'resident iff leased-or-pinned', per-unit, deterministic via injected clock."""

from livestack_node.lease import Capability
from livestack_node.residence import ResidenceController
from livestack_node.runtime import FakeRuntime


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def make_controller(clock=None, usage_ttl_seconds=0):
    clock = clock or FakeClock()
    runtime = FakeRuntime()
    units = {
        "asr": Capability(kind="asr", host_id="h", slots=64, lease_ttl_seconds=100),
        "diarize": Capability(kind="diarize", host_id="h", slots=64, lease_ttl_seconds=100),
    }
    return ResidenceController(
        "h", runtime, units, usage_ttl_seconds=usage_ttl_seconds, clock=clock
    ), runtime, clock


def test_acquire_warms_unit():
    ctrl, runtime, clock = make_controller()
    ctrl.acquire(kind="diarize", owner_id="o")
    assert runtime.resident() == ["diarize"]  # warm-on-acquire


def test_lease_expiry_evicts_unit():
    ctrl, runtime, clock = make_controller()
    ctrl.acquire(kind="diarize", owner_id="o")
    clock.t += 101
    ctrl.reconcile()  # the sweep analog
    assert runtime.resident() == []
    assert runtime.evict_calls == ["diarize"]


def test_heartbeat_keeps_resident():
    ctrl, runtime, clock = make_controller()
    lease = ctrl.acquire(kind="diarize", owner_id="o")
    clock.t += 99
    ctrl.heartbeat(lease.lease_id)
    clock.t += 99
    ctrl.reconcile()
    assert runtime.resident() == ["diarize"]


def test_release_evicts_unit():
    ctrl, runtime, clock = make_controller()
    lease = ctrl.acquire(kind="asr", owner_id="o")
    ctrl.release(lease.lease_id)
    assert runtime.resident() == []


def test_pinned_unit_survives_expiry_and_release():
    ctrl, runtime, clock = make_controller()
    ctrl.pin("asr")  # permanent residence
    assert runtime.resident() == ["asr"]
    clock.t += 10_000
    ctrl.reconcile()
    assert runtime.resident() == ["asr"]


def test_per_unit_eviction_keeps_pinned_neighbor():
    """The crux: evicting an idle diarize must NOT unload a pinned, co-resident
    asr (benchday's hot model)."""
    ctrl, runtime, clock = make_controller()
    ctrl.pin("asr")
    ctrl.acquire(kind="diarize", owner_id="o")
    assert runtime.resident() == ["asr", "diarize"]
    clock.t += 101  # diarize lease lapses; asr is pinned
    ctrl.reconcile()
    assert runtime.resident() == ["asr"]
    assert runtime.evict_calls == ["diarize"]


def test_unpin_then_evict():
    ctrl, runtime, clock = make_controller()
    ctrl.pin("asr")
    assert runtime.resident() == ["asr"]
    ctrl.pin("asr", pinned=False)  # no lease, no pin -> evict
    assert runtime.resident() == []


def test_usage_lease_keeps_unit_warm_then_evicts_when_idle():
    # usage_ttl>0: a used unit stays resident until the usage lease lapses.
    ctrl, runtime, clock = make_controller(usage_ttl_seconds=180)
    runtime.warm("diarize")          # simulate a lazy load by a GPU handler
    ctrl.note_usage("diarize")       # the use refreshes the usage lease
    ctrl.reconcile()
    assert runtime.resident() == ["diarize"]
    clock.t += 100
    ctrl.note_usage("diarize")       # used again within the TTL -> stays warm
    clock.t += 100
    ctrl.reconcile()
    assert runtime.resident() == ["diarize"]
    clock.t += 181                   # no use past the TTL -> idle-evicted
    ctrl.reconcile()
    assert runtime.resident() == []


def test_usage_ttl_zero_never_evicts():
    # usage_ttl<=0 mirrors POLYASR_IDLE_EVICT_SECONDS=0: used == resident forever.
    ctrl, runtime, clock = make_controller(usage_ttl_seconds=0)
    runtime.warm("diarize")
    ctrl.note_usage("diarize")
    clock.t += 10_000_000            # ~116 days
    ctrl.reconcile()
    assert runtime.resident() == ["diarize"]


def test_evict_only_mode_never_warms_but_evicts_idle():
    # Exclusive-residence servers (polytts): controller evicts idle units but
    # never proactively loads — loading stays lazy via the server's ensure().
    clock = FakeClock()
    runtime = FakeRuntime()
    units = {
        "qwen": Capability(kind="qwen", host_id="h", slots=64, lease_ttl_seconds=100),
        "voxcpm": Capability(kind="voxcpm", host_id="h", slots=64, lease_ttl_seconds=100),
    }
    ctrl = ResidenceController("h", runtime, units, usage_ttl_seconds=120,
                               proactive_warm=False, clock=clock)
    # An explicit lease does NOT eagerly load (no warm calls).
    ctrl.acquire(kind="qwen", owner_id="o")
    assert runtime.warm_calls == []
    assert runtime.resident() == []
    # The server lazily loads on real use; the use refreshes the usage lease.
    runtime.warm("qwen")
    ctrl.note_usage("qwen")
    clock.t += 121  # idle past the usage TTL
    ctrl.reconcile()
    assert runtime.resident() == []      # evicted
    assert runtime.evict_calls == ["qwen"]


def test_pinned_restore_when_slot_free_exclusive():
    # Exclusive one-model server (polytts): a pinned engine (benchday's qwen)
    # is preloaded and restored whenever the GPU slot frees, but never thrashes
    # an actively-leased other engine.
    clock = FakeClock()
    runtime = FakeRuntime()
    units = {
        "qwen": Capability(kind="qwen", host_id="h", slots=64, lease_ttl_seconds=100),
        "voxcpm": Capability(kind="voxcpm", host_id="h", slots=64, lease_ttl_seconds=100),
    }
    ctrl = ResidenceController("h", runtime, units, usage_ttl_seconds=120,
                               proactive_warm=False, clock=clock)
    ctrl.pin("qwen")
    assert runtime.resident() == ["qwen"]            # preloaded via pin (slot free)

    # A voxcpm request: exclusivity evicts qwen, voxcpm becomes resident + leased.
    runtime.evict("qwen"); runtime.warm("voxcpm")
    ctrl.note_usage("voxcpm")
    ctrl.reconcile()
    assert runtime.resident() == ["voxcpm"]          # qwen NOT restored (slot busy → no thrash)

    # voxcpm goes idle past its TTL: it evicts, slot frees, pinned qwen restored.
    clock.t += 121
    ctrl.reconcile()
    assert runtime.resident() == ["qwen"]


def test_status_reports_residence_and_leases():
    ctrl, runtime, clock = make_controller()
    ctrl.pin("asr")
    ctrl.acquire(kind="diarize", owner_id="o")
    status = ctrl.status()
    assert status["resident"] == ["asr", "diarize"]
    assert status["pinned"] == ["asr"]
    assert [l["kind"] for l in status["active_leases"]] == ["diarize"]
