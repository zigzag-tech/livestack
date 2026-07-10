"""LivestackCoordinator driving a REAL polycore.ModelManager — integration vectors
for lease-driven, pin-aware, per-unit residence. Deterministic via injected clock."""

import pytest

pytest.importorskip("livestack_node")
from livestack_node import ManagedUnit, ModelManager, ResidencyPolicy, noop_free  # noqa: E402

from livestack_node import LivestackCoordinator  # noqa: E402


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def mk(coload=True, usage_ttl=120, pin=None, names=("asr", "align", "diarize"), clock=None):
    clock = clock or FakeClock()
    coord = LivestackCoordinator("h", coload=coload, usage_ttl_seconds=usage_ttl, clock=clock)
    units = {}
    for n in names:
        pol = ResidencyPolicy.HARD_PIN if n == pin else ResidencyPolicy.UNPINNED
        units[n] = ManagedUnit(n, loader=(lambda n=n: f"model:{n}"),
                               freer=noop_free, residency_policy=pol)
    mgr = ModelManager(units, usage_ttl, coordinator=coord)
    return mgr, coord, clock


def test_ensure_loads_and_idle_evicts():
    mgr, coord, clock = mk()
    mgr.ensure("diarize")
    assert mgr.status()["resident"] == ["diarize"]
    clock.t += 121                       # past usage TTL
    assert mgr.maybe_evict() is True
    assert mgr.status()["resident"] == []


def test_usage_refresh_keeps_warm():
    mgr, coord, clock = mk()
    mgr.ensure("diarize")
    clock.t += 100
    mgr.ensure("diarize")                # re-use within TTL
    clock.t += 100
    mgr.maybe_evict()
    assert mgr.status()["resident"] == ["diarize"]


def test_hard_pin_survives_idle():
    mgr, coord, clock = mk(pin="asr")
    mgr.ensure("asr")
    clock.t += 10_000
    mgr.maybe_evict()
    assert mgr.status()["resident"] == ["asr"]


def test_per_unit_evict_keeps_pinned_neighbor_coload():
    mgr, coord, clock = mk(pin="asr")           # coload True
    mgr.ensure("asr"); mgr.ensure("diarize")
    assert mgr.status()["resident"] == ["asr", "diarize"]
    clock.t += 121
    mgr.maybe_evict()
    assert mgr.status()["resident"] == ["asr"]  # diarize evicted, pinned asr kept


def test_explicit_lease_holds_unit_past_usage_ttl():
    mgr, coord, clock = mk()
    coord.acquire_lease("diarize", owner_id="media-corpus", ttl_seconds=10_000)
    mgr.ensure("diarize")
    clock.t += 200                       # usage lease (120) expired; explicit (10000) active
    mgr.maybe_evict()
    assert mgr.status()["resident"] == ["diarize"]


def test_exclusive_evicts_other_and_restores_pin():
    mgr, coord, clock = mk(coload=False, pin="qwen", names=("qwen", "voxcpm"))
    mgr.ensure("qwen")
    assert mgr.status()["resident"] == ["qwen"]
    mgr.ensure("voxcpm")                  # exclusive: evicts qwen
    assert mgr.status()["resident"] == ["voxcpm"]
    clock.t += 121                        # voxcpm idle
    mgr.maybe_evict()
    assert mgr.status()["resident"] == ["qwen"]  # voxcpm evicted, pinned qwen restored


def test_request_evict_honours_pin():
    mgr, coord, clock = mk(pin="asr")
    mgr.ensure("asr"); mgr.ensure("diarize")
    mgr.request_evict("asr")             # pinned -> ignored
    mgr.request_evict("diarize")         # unpinned -> evicted
    assert mgr.status()["resident"] == ["asr"]
