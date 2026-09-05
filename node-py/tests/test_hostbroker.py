"""HostBroker: cross-process preemption on one shared GPU (the real single-host
case — polyasr/polytts/chipgen are separate processes). Fake peers stand in for the
three servers; we assert the broker dispatches the right warm/evict calls."""
from livestack_node.measure import measure_footprint
from livestack_node.hostbroker import HostBroker
from livestack_node.planner import Device, Grant, Load, Unit, Placement, Request, Residency
from livestack_node.ledger import validate


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


# ---------------------------------------------------------------------------
# Phase 1 of `_plans/fleet-broker.md`: the fleet broker, observe-only.
# ---------------------------------------------------------------------------

import time as _time

from livestack_node.hostbroker import peer_key as _peer_key
from livestack_node.ledger import JsonlLedger
from livestack_node.membership import MembershipPolicy as _MPolicy
from livestack_node.planner import Device as _Device, Placement as _Placement
from livestack_node.planner import Residency as _Residency, Unit as _Unit


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


class _FleetPeer:
    """A node whose reachability, cost and reported load the test controls."""

    def __init__(self, base, host="h", device="h/gpu0", kind="asr",
                 units_delay=0.0, ready=True, load=None, resident=True):
        self.base = base
        self.host_id = host
        self.device_id = device
        self._kind = kind
        self._unit = _Unit(kind, {"vram_bytes": 4}, priority=10,
                           residency=_Residency.UNPINNED)
        self.up = True
        self.resident = resident
        self.units_delay = units_delay
        self.ready = ready
        self._load = load
        self.warmed, self.evicted = [], []

    def units(self):
        if self.units_delay:
            _time.sleep(self.units_delay)
        if not self.up:
            raise ConnectionError("Connection refused")
        return {self._kind: self._unit}

    def placements(self):
        return ([_Placement(self._kind, self.device_id, busy=False)]
                if self.resident else [])

    def device_memory(self):
        return None

    def device_capacity(self):
        return None

    def capability(self):
        if not self.up:
            raise ConnectionError("Connection refused")
        out = {"kind": self._kind, "host_id": self.host_id,
               "device_id": self.device_id, "units": [self._kind],
               "ready": self.ready, "detail": "resident" if self.ready else "cold",
               "labels": {"arch": "cuda"}}
        if self._load is not None:
            out["load"] = self._load
        return out

    def warm(self, kind):
        self.warmed.append(kind)

    def evict(self, kind):
        self.evicted.append(kind)


def _fleet_broker(peers, **kw):
    return HostBroker(devices=None, peers=peers, clock=_time.monotonic,
                      membership=_MPolicy(suspect_after_s=60, mia_after_s=900),
                      **kw)


def test_observe_only_plans_but_dispatches_nothing():
    """The whole safety property: one card, one master. A fleet broker pointed
    at remote nodes must not fight the host brokers that own them — and the
    plan is still the product, so it is computed and returned, just not sent."""
    cold = _FleetPeer("http://a/livestack", kind="asr", resident=False)
    cold._unit = _Unit("asr", {"vram_bytes": 4}, priority=10,
                       residency=_Residency.HARD_PIN)

    observer = _fleet_broker([cold], dispatch=False)
    p = observer.plan_and_apply([])
    assert p.of(Load), "the plan is still the product of a fleet broker"
    assert cold.warmed == [] and cold.evicted == [], "observe-only dispatched"

    # The same fixture under the default flag DOES dispatch, so the difference
    # is the flag and not the setup.
    applier = _fleet_broker([cold])
    applier.plan_and_apply([])
    assert cold.warmed == ["asr"]


def test_probe_ms_measures_the_snapshot_it_already_pays_for():
    slow = _FleetPeer("http://slow/livestack", host="far", device="far/gpu0",
                      units_delay=0.05)
    fast = _FleetPeer("http://fast/livestack", host="near", device="near/gpu0")
    b = _fleet_broker([slow, fast], dispatch=False)
    b.snapshot([])
    assert b.probe_ms["http://slow/livestack"] >= 50
    assert b.probe_ms["http://fast/livestack"] < 50


def test_probe_ms_is_an_ewma_that_converges():
    p = _FleetPeer("http://p/livestack")
    b = _fleet_broker([p], dispatch=False)
    b._record_probe_ms("k", 1000.0)
    assert b.probe_ms["k"] == 1000.0
    for _ in range(50):
        b._record_probe_ms("k", 10.0)
    assert 9.9 <= b.probe_ms["k"] <= 11.0, b.probe_ms["k"]


def test_the_fleet_view_groups_by_host_and_carries_load():
    a = _FleetPeer("http://a/livestack", host="zz-tower0", device="zz-tower0/aaaa",
                   load={"in_flight": 0, "pressure": 0.51, "in_flight_source": "server",
                         "device": {"capacity": 25296044032, "free": 12345678901}})
    b = _FleetPeer("http://b/livestack", host="xc-tower-ubuntu",
                   device="xc-tower-ubuntu/bbbb")
    br = _fleet_broker([a, b], dispatch=False)
    br.snapshot([])
    view = br.fleet_view()

    assert view["dispatch"] is False
    assert view["peers"] == 2
    assert sorted(view["hosts"]) == ["xc-tower-ubuntu", "zz-tower0"]
    row = view["hosts"]["zz-tower0"]["nodes"][0]
    assert row["state"] == "fresh"
    assert row["ready"] is True
    assert row["device_id"] == "zz-tower0/aaaa"
    assert row["load"]["pressure"] == 0.51
    assert row["device_mem"]["free"] == 12345678901
    assert "probe_ms" in row
    assert row["units"][0]["kind"] == "asr"
    # b reports no load at all, and absent must stay absent: a consumer reads it
    # as NO OPINION, never as idle.
    assert "load" not in view["hosts"]["xc-tower-ubuntu"]["nodes"][0]


def test_an_unreachable_peer_is_a_row_not_a_gap():
    """A view that omits what it cannot reach can only report health, which is
    not what anyone opens it to find out."""
    gone = _FleetPeer("http://gone/livestack", host="zz-tower0")
    gone.up = False
    br = _fleet_broker([gone], dispatch=False)
    br.snapshot([])
    view = br.fleet_view()
    rows = [r for h in view["hosts"].values() for r in h["nodes"]]
    assert len(rows) == 1
    assert rows[0]["peer"] == "http://gone/livestack"
    assert "unseen_seconds" in rows[0]


def test_the_capability_cache_bounds_what_a_poll_surface_costs():
    """`/fleet` is polled. Without a TTL a 1 Hz UI would probe every node in the
    fleet every second, and a probe to Nanjing costs 0.5-1.5 s."""
    calls = []
    p = _FleetPeer("http://p/livestack")
    real = p.capability
    p.capability = lambda: (calls.append(1), real())[1]
    br = _fleet_broker([p], dispatch=False)
    br.capability_ttl_s = 100
    br.snapshot([])
    for _ in range(20):
        br.fleet_view()
    assert len(calls) == 1, f"probed {len(calls)} times for 20 polls"


def test_a_membership_transition_emits_one_ledger_record_not_one_per_tick(tmp_path):
    """A per-tick emitter is how a 92,089-line log happened. Riding the same
    edge the log line rides makes 'once per transition' structural."""
    led = JsonlLedger(str(tmp_path / "fleet.jsonl"))
    gone = _FleetPeer("http://gone/livestack", host="h")
    br = HostBroker(devices=None, peers=[gone], clock=_time.monotonic,
                    membership=_MPolicy(suspect_after_s=0.01, mia_after_s=900),
                    dispatch=False, ledger=led, emitter="fleet-broker",
                    emitter_id="xc-tower-ubuntu:8801")
    br.snapshot([])                        # fresh
    gone.up = False
    _time.sleep(0.02)
    for _ in range(50):                    # 50 reconcile ticks with it down
        br.roster.tick()

    recs = led.read()
    observes = [r for r in recs if r["decision"] == "observe"]
    assert len(observes) == 1, f"{len(observes)} records for one transition"
    r = observes[0]
    assert r["emitter"] == "fleet-broker"
    assert r["emitter_id"] == "xc-tower-ubuntu:8801"
    assert r["candidates"][0]["state"] == "suspect"
    assert "fresh -> suspect" in r["candidates"][0]["reason"]
    assert validate(r) == []


def test_a_plan_with_actions_emits_a_record_with_the_candidates_around_it(tmp_path):
    """`[hostbroker] evict llm@...: relieve measured over-budget pressure` says
    what happened and never said what else was possible, so a reader could not
    tell a correct eviction from a wrong one. The reason string was already
    good; the candidate rows are what was missing."""
    led = JsonlLedger(str(tmp_path / "host.jsonl"))
    peer = _FleetPeer("http://a/livestack", kind="asr", resident=False)
    peer._unit = _Unit("asr", {"vram_bytes": 4}, priority=10,
                       residency=_Residency.HARD_PIN)
    br = HostBroker(devices=[_Device("h/gpu0", "h", capacity={"vram_bytes": 24})],
                    peers=[peer], clock=_time.monotonic, ledger=led,
                    emitter_id="h:8799")
    br.plan_and_apply([])

    recs = [r for r in led.read() if r["decision"] in ("load", "evict", "grant")]
    assert recs, "a plan with actions emitted nothing"
    r = recs[0]
    assert r["emitter"] == "host-broker"
    assert r["chosen"] == "asr"
    assert r["reason"]
    assert all(c["reason"] for c in r["candidates"])
    assert validate(r) == []


def test_a_plan_with_no_actions_emits_nothing(tmp_path):
    """Rate is bounded by the plan, not by the clock — a reconcile tick that
    decides nothing must write nothing."""
    led = JsonlLedger(str(tmp_path / "host.jsonl"))
    peer = _FleetPeer("http://a/livestack", kind="asr")
    br = HostBroker(devices=[_Device("h/gpu0", "h", capacity={"vram_bytes": 24})],
                    peers=[peer], clock=_time.monotonic, ledger=led)
    for _ in range(20):
        br.plan_and_apply([])
    assert [r for r in led.read() if r["decision"] != "observe"] == []


def test_a_grant_now_carries_a_reason():
    """Every other action already had one. A grant did not, so the ledger could
    record WHAT was admitted and never why."""
    peer = _FleetPeer("http://a/livestack", kind="asr")
    br = HostBroker(devices=[_Device("h/gpu0", "h", capacity={"vram_bytes": 24})],
                    peers=[peer], clock=_time.monotonic)
    p = br.plan_and_apply([Request("r1", "asr", created_at=0)])
    grants = p.of(Grant)
    assert grants and grants[0].reason in ("resident", "loaded on demand")


def test_observe_only_does_not_reclaim_either():
    """Reclaim is a WRITE to another process's allocator. It is not a warm or an
    evict, which is exactly why it is the exception that gets forgotten —
    "observe-only" reads as being about the planner."""
    class _Leaky(_FleetPeer):
        leak = {"unexplained_bytes": 14_700_000_000}

        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.reclaims = 0

        def reclaim(self):
            self.reclaims += 1
            return {"freed_bytes": 0}

    p = _Leaky("http://leaky/livestack")
    assert _fleet_broker([p], dispatch=False).sweep_leaks() == []
    assert p.reclaims == 0
    _fleet_broker([p]).sweep_leaks()
    assert p.reclaims == 1, "a HOST broker must still reclaim its own host"


def test_a_dead_peer_is_not_asked_to_reclaim_every_tick():
    """Found live on 2026-09-05: `RestPeer.leak` is a property that GETs
    /residence, so on a down peer it raised BEFORE `_last_reclaim` was stamped
    and the throttle was never armed. One "reclaim failed" line per reconcile
    tick, forever — ~17k lines a day for one dead chipgen. The 92,089-line shape
    membership was built to end, on a path membership did not cover."""
    class _Angry(_FleetPeer):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.touches = 0

        @property
        def leak(self):
            self.touches += 1
            raise ConnectionError("Connection refused")

    clock = Clock(1000.0)
    p = _Angry("http://dead/livestack")
    p.up = False
    br = HostBroker(devices=None, peers=[p], clock=clock,
                    membership=_MPolicy(suspect_after_s=45, mia_after_s=600))
    for _ in range(120):                # 120 reconcile ticks, 5 s apart
        br.snapshot([])                 # marks it probed -> suspect -> mia
        br.sweep_leaks(now=clock.t)
        clock.advance(5)
    # One attempt while it was still `fresh` is legitimate — that is the probe
    # that discovers it is gone. What must not happen is 120 of them.
    assert p.touches <= 1, (
        f"asked a peer membership already calls absent to reclaim {p.touches} times")


# -- Phase 2a: the links matrix ----------------------------------------------

def test_links_are_measured_per_host_and_keyed_by_the_remotes_own_id(monkeypatch):
    """`probe_ms` is a STAR — distance from where this broker sits. A client in
    Nanjing must not be ranked by Vaughan's view of Nanjing, so each host
    measures its own row and the fleet broker collects them."""
    seen = []

    def fake_http(url, body=None, timeout=5):
        seen.append(url)
        if "slow" in url:
            _time.sleep(0.05)
            return {"host_id": "zz-tower0", "peers": [],
                    "links": {"xc-mac-studio": 515.0}}
        return {"host_id": "xc-mac-studio", "peers": [], "links": {}}

    monkeypatch.setattr("livestack_node.hostbroker._http", fake_http)
    br = _fleet_broker([], dispatch=False)
    br.host_id = "xc-tower-ubuntu"
    br.link_peers = ["http://slow:8799", "http://near:8799"]
    br.measure_links()

    assert all(u.endswith("/peers") for u in seen), \
        "collection must use the CHEAP endpoint, not /status"
    assert br.link_ms["zz-tower0"] >= 50
    assert br.link_ms["xc-mac-studio"] < 50
    # The remote's OWN row comes back with it: one GET buys the timing and its
    # slice of the matrix.
    assert br.peer_links["zz-tower0"] == {"xc-mac-studio": 515.0}

    matrix = br.links_view()
    assert matrix["xc-tower-ubuntu"]["zz-tower0"] >= 50
    assert matrix["zz-tower0"]["xc-mac-studio"] == 515.0


def test_a_link_that_cannot_be_measured_is_left_alone_not_zeroed(monkeypatch):
    """No opinion is the correct answer for a pair we failed to measure. A zero
    would read as 'adjacent', which is the direction that sends work across an
    ocean."""
    def angry(url, body=None, timeout=5):
        raise ConnectionError("Connection refused")

    monkeypatch.setattr("livestack_node.hostbroker._http", angry)
    br = _fleet_broker([], dispatch=False)
    br.link_peers = ["http://gone:8799"]
    assert br.measure_links() == {}
    assert br.links_view() == {}


def test_an_operator_can_name_a_host_whose_broker_is_too_old_to_say(monkeypatch):
    monkeypatch.setattr("livestack_node.hostbroker._http",
                        lambda url, body=None, timeout=5: {"peers": []})
    br = _fleet_broker([], dispatch=False)
    br.link_peers = ["zz-tower0=http://100.64.0.3:8799"]
    br.measure_links()
    assert "zz-tower0" in br.link_ms, \
        "without a name the matrix keys by URL and vantage=host:<id> never resolves"


def test_the_fleet_view_carries_each_hosts_own_link_row(monkeypatch):
    monkeypatch.setattr(
        "livestack_node.hostbroker._http",
        lambda url, body=None, timeout=5: {
            "host_id": "zz-tower0", "peers": [],
            "links": {"xc-tower-ubuntu": 540.0}})
    p = _FleetPeer("http://n/livestack", host="xc-tower-ubuntu",
                   device="xc-tower-ubuntu/aaaa")
    br = _fleet_broker([p], dispatch=False)
    br.host_id = "xc-tower-ubuntu"
    br.link_peers = ["http://100.64.0.3:8799"]
    br.snapshot([])
    br.measure_links()
    view = br.fleet_view()
    assert view["vantage_host"] == "xc-tower-ubuntu"
    assert "zz-tower0" in view["hosts"]["xc-tower-ubuntu"]["links"]
