"""HostBroker: cross-process preemption on one shared GPU (the real single-host
case — polyasr/polytts/chipgen are separate processes). Fake peers stand in for the
three servers; we assert the broker dispatches the right warm/evict calls."""
from livestack_node.measure import measure_footprint
from livestack_node.hostbroker import HostBroker
from livestack_node.planner import Device, Unit, Placement, Request, Residency


class FakePeer:
    def __init__(self, host, device, unit, resident=False, busy=False):
        self.host_id = host
        self.device_id = device
        self._unit = unit
        self._resident = resident
        self._busy = busy
        self.calls = []

    def units(self):
        return {self._unit.kind: self._unit}

    def placements(self):
        if not self._resident:
            return []
        return [Placement(self._unit.kind, self.device_id, loaded_at=0, busy=self._busy)]

    def warm(self, kind):
        self.calls.append(("warm", kind)); self._resident = True

    def evict(self, kind):
        self.calls.append(("evict", kind)); self._resident = False


def make_host(tts_busy=False, chip_busy=False):
    dev = Device("gpu0", "tower0", capacity={"vram": 24}, reserved={"vram": 1})
    asr = FakePeer("tower0", "gpu0",
                   Unit("align", {"vram": 10}, priority=10, residency=Residency.HARD_PIN,
                        min_resident=1, reload_cost=8))
    tts = FakePeer("tower0", "gpu0",
                   Unit("tts", {"vram": 9}, priority=20, residency=Residency.SOFT_PIN,
                        reload_cost=6),
                   resident=True, busy=tts_busy)
    chip = FakePeer("tower0", "gpu0",
                    Unit("chipgen", {"vram": 5}, priority=30, residency=Residency.UNPINNED,
                         reload_cost=4),
                    resident=True, busy=chip_busy)
    return HostBroker([dev], [asr, tts, chip], clock=lambda: 1000.0), asr, tts, chip


def test_align_request_preempts_idle_chipgen_in_other_process():
    broker, asr, tts, chip = make_host()
    dev = broker.admit(Request("r1", "align", created_at=1000))
    assert dev == "gpu0"
    assert ("evict", "chipgen") in chip.calls    # broker told the chipgen PROCESS to evict
    assert ("warm", "align") in asr.calls        # and the asr process to warm
    assert ("evict", "tts") not in tts.calls     # more-important TTS left alone


class DownPeer:
    """A model server that is unreachable this cycle: every accessor raises."""
    host_id = "tower0"
    device_id = "gpu0"

    def units(self):
        raise ConnectionError("peer down")

    def placements(self):
        raise ConnectionError("peer down")

    def device_memory(self):
        raise ConnectionError("peer down")

    def device_capacity(self):
        raise ConnectionError("peer down")

    def warm(self, kind):
        raise ConnectionError("peer down")

    def evict(self, kind):
        raise ConnectionError("peer down")


def test_broker_tolerates_down_peer_and_still_evicts():
    # One of the three model servers is unreachable this cycle. It must NOT blind the
    # whole arbiter (which would make the caller fail-open and OOM): the broker skips
    # the down peer and still plans over the survivors — evicting the idle UNPINNED
    # chipgen (in its own process) to admit the HARD_PIN align (in another).
    dev = Device("gpu0", "tower0", capacity={"vram": 16}, reserved={"vram": 1})
    asr = FakePeer("tower0", "gpu0",
                   Unit("align", {"vram": 12}, priority=10, residency=Residency.HARD_PIN,
                        min_resident=1, reload_cost=8))
    chip = FakePeer("tower0", "gpu0",
                    Unit("chipgen", {"vram": 5}, priority=30, residency=Residency.UNPINNED,
                         reload_cost=4),
                    resident=True)
    broker = HostBroker([dev], [asr, DownPeer(), chip], clock=lambda: 1000.0)
    granted = broker.admit(Request("r1", "align", created_at=1000))
    assert granted == "gpu0"                      # planned + granted despite the down peer
    assert ("evict", "chipgen") in chip.calls     # idle UNPINNED still evicted over the survivor
    assert ("warm", "align") in asr.calls


def test_align_defers_when_lower_priority_all_busy():
    broker, asr, tts, chip = make_host(tts_busy=True, chip_busy=True)
    dev = broker.admit(Request("r1", "align", created_at=1000))
    assert dev is None                            # 时间换空间: wait, don't interrupt busy work
    assert ("evict", "chipgen") not in chip.calls
    assert ("evict", "tts") not in tts.calls


def test_measure_footprint_captures_peak_activation():
    # weights 10 GB but a transient 12 GB peak during a run -> footprint = 12 GB.
    class M:
        def __init__(self): self._a = 0; self._p = 0
        def reset_peak(self): self._p = self._a
        def allocated(self): return self._a
        def max_allocated(self): return self._p
    m = M()

    def load():
        m._a = 10_000_000_000; m._p = 10_000_000_000; return "model"

    def run(model):
        m._p = 12_000_000_000        # activation high-water mark

    model, fp = measure_footprint(load, run, meter=m)
    assert model == "model"
    assert fp["vram_bytes"] == 12_000_000_000


def test_measured_capacity_autosizes_device():
    # No fixed device, no device_config: the default budget is a tiny 12 GB, but the
    # peer REPORTS a real 40 GB device. Measured capacity must auto-size the device so
    # a 30 GB unit fits, instead of being wrongly rejected against the 12 GB guess.
    class MeteredPeer:
        host_id = "tower0"; device_id = "tower0/gpu0"
        def __init__(self):
            self.calls = []
            self._unit = Unit("big", {"vram_bytes": 30}, priority=20,
                              residency=Residency.UNPINNED)
        def units(self): return {"big": self._unit}
        def placements(self): return []
        def device_memory(self): return {"vram_bytes": 38}
        def device_capacity(self): return {"vram_bytes": 40}
        def warm(self, kind): self.calls.append(("warm", kind))
        def evict(self, kind): self.calls.append(("evict", kind))

    peer = MeteredPeer()
    broker = HostBroker(devices=None, peers=[peer], clock=lambda: 1000.0,
                        default_capacity={"vram_bytes": 12, "reserved": 0})
    dev = broker.admit(Request("r1", "big", created_at=1000))
    assert dev == "tower0/gpu0"
    assert ("warm", "big") in peer.calls


def test_hosted_backends_are_configured_not_discovered():
    """Nothing on the host reports a vendor endpoint, so a hosted device can only
    come from configuration — and it must appear alongside the discovered GPUs
    rather than instead of them."""
    from livestack_node.hostbroker import HostBroker
    b = HostBroker(device_config={
        "gpu0": {"vram_bytes": 24_000_000_000, "reserved": 2_000_000_000},
        "qwen-sg": {"hosted": True, "concurrency": 8, "cost_bias": -1.0,
                    "labels": {"region": "apac-sg"}},
    })
    devs = {d.id: d for d in b._resolve_devices({"gpu0": "tower0"})}
    assert set(devs) == {"gpu0", "qwen-sg"}
    assert devs["gpu0"].hosted is False
    q = devs["qwen-sg"]
    assert q.hosted and q.cost_bias == -1.0
    assert q.capacity == {"concurrency": 8.0}
    assert q.labels["region"] == "apac-sg"
    assert q.available is True

    # Health gates it without touching config: mark it down and it stops being
    # offered, which is the whole fallback story.
    b.hosted_available["qwen-sg"] = False
    devs2 = {d.id: d for d in b._resolve_devices({"gpu0": "tower0"})}
    assert devs2["qwen-sg"].available is False


class _LeakyPeer:
    """A node that has evicted everything and still holds the card — the exact
    shape of the 2026-08-04 outage."""
    def __init__(self, base="http://leaky", leak=True):
        self.base = base
        self._leak = {"unexplained_bytes": 14_700_000_000,
                      "reclaimable_bytes": 14_700_000_000} if leak else None
        self.reclaims = 0

    @property
    def leak(self):
        return self._leak

    def reclaim(self):
        self.reclaims += 1
        freed = int(self._leak["reclaimable_bytes"]) if self._leak else 0
        self._leak = None                       # the pool came back
        return {"freed_bytes": freed}


def test_a_leaking_peer_is_asked_to_return_its_pool():
    """Detection alone could not fix the outage: Harmony's only lever is evicting
    units, and the leaked memory belonged to no unit. Reclaim is the lever."""
    from livestack_node.hostbroker import HostBroker
    b = HostBroker()
    peer = _LeakyPeer()
    b.register_peer(peer)

    acted = b.sweep_leaks(now=1000.0)
    assert peer.reclaims == 1
    assert acted[0]["freed_bytes"] == 14_700_000_000
    assert acted[0]["unexplained_bytes"] == 14_700_000_000


def test_a_healthy_peer_is_left_alone():
    """Reclaim touches the GPU executor. A sweep that fires on healthy nodes
    would contend with real work every cycle."""
    from livestack_node.hostbroker import HostBroker
    b = HostBroker()
    peer = _LeakyPeer(leak=False)
    b.register_peer(peer)
    assert b.sweep_leaks(now=1000.0) == []
    assert peer.reclaims == 0


def test_reclaim_is_throttled_per_peer():
    """A node whose pool does not come back is reporting a real leak, not a
    stale cache — worth a log line, not a tight retry loop."""
    from livestack_node.hostbroker import HostBroker
    b = HostBroker()
    b.reclaim_interval_s = 120.0
    peer = _LeakyPeer()
    peer._leak_sticky = True

    b.register_peer(peer)
    b.sweep_leaks(now=1000.0)
    peer._leak = {"unexplained_bytes": 14_700_000_000, "reclaimable_bytes": 0}  # still leaking
    b.sweep_leaks(now=1060.0)          # inside the window
    assert peer.reclaims == 1, "must not hammer the GPU executor"
    b.sweep_leaks(now=1200.0)          # past it
    assert peer.reclaims == 2


def test_one_peer_failing_does_not_stop_the_sweep():
    from livestack_node.hostbroker import HostBroker

    class _Broken(_LeakyPeer):
        def reclaim(self):
            raise RuntimeError("connection refused")

    b = HostBroker()
    broken, ok = _Broken(base="http://broken"), _LeakyPeer(base="http://ok")
    b.register_peer(broken)
    b.register_peer(ok)
    acted = b.sweep_leaks(now=1000.0)
    assert ok.reclaims == 1, "a dead peer must not block its neighbours"
    assert [a["peer"] for a in acted] == ["http://ok"]


# --- hosted BUILD hosts (the peerless case) ---------------------------------
# A build host is a hosted device with a concurrency ceiling; no peer speaks
# for it, so the broker's own unit config + lease ledger are the whole story.

def make_build_broker(device_config, clock=None):
    return HostBroker(device_config=device_config,
                      extra_units={"build": Unit("build", {}, priority=20)},
                      clock=clock or (lambda: 1000.0))


def test_admit_build_on_a_peerless_broker():
    """Zero peers: the kind comes from config, the device comes from config,
    and the grant must still land — that is the whole BUILD-host case."""
    b = make_build_broker({"buildhost-a": {"hosted": True, "concurrency": 1,
                                           "cost_bias": 0,
                                           "labels": {"arch": "linux/amd64"}}})
    assert b.admit(Request("r1", "build", created_at=1000)) == "buildhost-a"


def test_selector_picks_the_matching_arch():
    b = make_build_broker({
        "buildhost-amd": {"hosted": True, "concurrency": 1,
                          "labels": {"arch": "linux/amd64"}},
        "buildhost-arm": {"hosted": True, "concurrency": 1,
                          "labels": {"arch": "linux/arm64"}},
    })
    dev = b.admit(Request("r1", "build", created_at=1000,
                          selector={"arch": "linux/arm64"}))
    assert dev == "buildhost-arm"


def test_cheaper_hosted_device_wins():
    # Sorted order puts buildhost-a first; the grant must still go to b, or the
    # ordering is accidental rather than the bias.
    b = make_build_broker({
        "buildhost-a": {"hosted": True, "concurrency": 1, "cost_bias": 2},
        "buildhost-b": {"hosted": True, "concurrency": 1, "cost_bias": 0},
    })
    assert b.admit(Request("r1", "build", created_at=1000)) == "buildhost-b"


def test_an_unhealthy_device_is_skipped():
    b = make_build_broker({
        "buildhost-a": {"hosted": True, "concurrency": 1, "cost_bias": 0},
        "buildhost-b": {"hosted": True, "concurrency": 1, "cost_bias": 2},
    })
    b.set_hosted_available("buildhost-a", False)
    assert b.admit(Request("r1", "build", created_at=1000)) == "buildhost-b"


def test_concurrency_ceiling_until_release():
    """concurrency: 1 means ONE build at a time. The second admit must defer
    until the first lease is released — this is why the ledger exists."""
    b = make_build_broker({"buildhost-a": {"hosted": True, "concurrency": 1}})
    lease = b.hosted_checkout("buildhost-a", "build", "ci", now=1000)
    assert b.admit(Request("r1", "build", created_at=1000)) is None
    assert b.hosted_release(lease) is True
    assert b.admit(Request("r2", "build", created_at=1000)) == "buildhost-a"


def test_a_lease_older_than_the_ttl_stops_counting():
    """A leaseholder that died mid-build cannot release; the TTL is how its
    slot comes back on its own."""
    t = [1000.0]
    b = make_build_broker({"buildhost-a": {"hosted": True, "concurrency": 1}},
                          clock=lambda: t[0])
    b.hosted_lease_ttl_s = 120.0
    b.hosted_checkout("buildhost-a", "build", "ci", now=t[0])
    assert b.admit(Request("r1", "build", created_at=t[0])) is None
    t[0] += 121.0                        # past the TTL, no heartbeat
    assert b.admit(Request("r2", "build", created_at=t[0])) == "buildhost-a"


def test_a_heartbeat_keeps_the_lease_alive():
    t = [1000.0]
    b = make_build_broker({"buildhost-a": {"hosted": True, "concurrency": 1}},
                          clock=lambda: t[0])
    b.hosted_lease_ttl_s = 120.0
    lease = b.hosted_checkout("buildhost-a", "build", "ci", now=t[0])
    t[0] += 100.0
    assert b.hosted_heartbeat(lease, now=t[0]) is True
    t[0] += 100.0                        # 200 s old, but refreshed at 100
    assert b.admit(Request("r1", "build", created_at=t[0])) is None
    t[0] += 121.0                        # nothing since 1100 -> expired
    assert b.admit(Request("r2", "build", created_at=t[0])) == "buildhost-a"
    assert b.hosted_heartbeat("nope") is False
