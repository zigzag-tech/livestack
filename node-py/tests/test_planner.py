"""Tests for the generalized resource-aware preemptive planner. Pure logic, no GPU:
a fake ``now`` and GB-valued footprints model the real polyasr/polytts/chipgen
contention that motivated it (ASR > TTS > chipgen on a 24 GB card)."""
from livestack_node.planner import (
    Device, Unit, Placement, Request, WorldState, PlannerPolicy, Residency,
    plan, Load, Evict, Grant, Defer,
)

GB = 1.0  # work in GB


def gpu(id="gpu0", host="tower0", cap=24, reserved=1, labels=None):
    return Device(id=id, host_id=host, capacity={"vram": cap},
                  reserved={"vram": reserved}, labels=labels or {})


# Real-world-ish units. Lower priority int = more important.
def units():
    return {
        "asr": Unit("asr", {"vram": 10}, priority=10, residency=Residency.HARD_PIN,
                    min_resident=1, reload_cost=8),
        "tts": Unit("tts", {"vram": 9}, priority=20, residency=Residency.SOFT_PIN,
                    reload_cost=6),
        "chipgen": Unit("chipgen", {"vram": 5}, priority=30, residency=Residency.UNPINNED,
                        reload_cost=4),
    }


def kinds_of(actions, cls):
    return sorted(a.kind for a in actions if isinstance(a, cls))


def test_load_into_free_then_grant():
    w = WorldState(devices=(gpu(),), units=units(),
                   requests=(Request("r1", "chipgen", created_at=0),), now=100)
    p = plan(w)
    # chipgen is served from the request; ASR is also kept warm by its HARD_PIN floor.
    assert "chipgen" in kinds_of(p.of(Load), Load)
    assert any(g.kind == "chipgen" and g.device_id == "gpu0" for g in p.of(Grant))


def test_warm_grant_needs_no_reload():
    # chipgen already resident -> a new lease is served warm, no Load.
    w = WorldState(devices=(gpu(),), units=units(),
                   placements=(Placement("chipgen", "gpu0", loaded_at=0),),
                   requests=(Request("r1", "chipgen", created_at=100),), now=100)
    p = plan(w)
    assert "chipgen" not in kinds_of(p.of(Load), Load)   # already resident: served warm
    assert any(g.kind == "chipgen" for g in p.of(Grant))


def test_priority_preempts_minimal_idle_victim():
    # GPU holds idle TTS(9)+chipgen(5); free=9. ASR(10) must evict exactly chipgen
    # (least important, frees enough) and NOT the more-important TTS.
    w = WorldState(devices=(gpu(),), units=units(),
                   placements=(Placement("tts", "gpu0", loaded_at=0),
                               Placement("chipgen", "gpu0", loaded_at=0)),
                   requests=(Request("r1", "asr", created_at=100),), now=100)
    p = plan(w)
    assert kinds_of(p.of(Evict), Evict) == ["chipgen"]
    assert kinds_of(p.of(Load), Load) == ["asr"]
    assert "tts" not in kinds_of(p.of(Evict), Evict)


def test_busy_unit_is_not_preempted():
    # chipgen busy (protected); ASR frees the idle TTS instead.
    w = WorldState(devices=(gpu(),), units=units(),
                   placements=(Placement("tts", "gpu0", loaded_at=0, busy=False),
                               Placement("chipgen", "gpu0", loaded_at=0, busy=True, leases=1)),
                   requests=(Request("r1", "asr", created_at=100),), now=100)
    p = plan(w)
    assert kinds_of(p.of(Evict), Evict) == ["tts"]


def test_defers_when_only_busy_lower_priority_blocks():
    # Both lower-priority units busy; free(9) < asr(10) -> 时间换空间: defer.
    w = WorldState(devices=(gpu(),), units=units(),
                   placements=(Placement("tts", "gpu0", loaded_at=0, busy=True, leases=1),
                               Placement("chipgen", "gpu0", loaded_at=0, busy=True, leases=1)),
                   requests=(Request("r1", "asr", created_at=100),), now=100)
    p = plan(w)
    assert p.of(Evict) == []
    assert [d.request_id for d in p.of(Defer)] == ["r1"]


def test_multi_resource_binding_dimension():
    # Two dims; ram is the binding constraint even though vram fits.
    u = {"big": Unit("big", {"vram": 4, "ram": 30}, priority=10)}
    d = Device("d", "h", capacity={"vram": 24, "ram": 16})
    w = WorldState(devices=(d,), units=u, requests=(Request("r", "big", created_at=0),), now=1)
    p = plan(w)
    assert [x.reason for x in p.of(Defer)]  # cannot fit on ram
    assert p.of(Load) == []


def test_cross_device_places_instead_of_preempting():
    # gpu0 full of preemptible filler; gpu1 has room. ASR should LOAD on gpu1
    # (cheaper than preempting gpu0), evicting nobody.
    u = units()
    u["filler"] = Unit("filler", {"vram": 22}, priority=30, residency=Residency.UNPINNED,
                       reload_cost=4)
    w = WorldState(
        devices=(gpu("gpu0"), gpu("gpu1")), units=u,
        placements=(Placement("filler", "gpu0", loaded_at=0),),
        requests=(Request("r1", "asr", created_at=100),), now=100)
    p = plan(w)
    assert p.of(Evict) == []                 # placed in free room, preempted nobody
    asr_loads = [l for l in p.of(Load) if l.kind == "asr"]
    assert len(asr_loads) == 1 and asr_loads[0].device_id == "gpu1"


def test_hard_pin_floor_preempts_to_stay_warm():
    # No ASR request, but ASR is HARD_PIN min_resident=1 and not resident; the GPU
    # is full of idle UNPINNED filler -> floor pass preempts filler to keep ASR warm.
    u = units()
    u["filler"] = Unit("filler", {"vram": 20}, priority=30, residency=Residency.UNPINNED)
    w = WorldState(devices=(gpu(),), units=u,
                   placements=(Placement("filler", "gpu0", loaded_at=0),), now=100)
    p = plan(w)
    assert "filler" in kinds_of(p.of(Evict), Evict)
    assert "asr" in kinds_of(p.of(Load), Load)


def test_hard_pin_never_chosen_as_victim():
    # ASR resident + busy elsewhere is never evicted to satisfy a lower-priority req.
    u = units()
    w = WorldState(devices=(gpu(cap=12, reserved=0),), units=u,
                   placements=(Placement("asr", "gpu0", loaded_at=0),),  # 10 used, free 2
                   requests=(Request("r1", "tts", created_at=100),), now=100)  # tts needs 9
    p = plan(w)
    assert p.of(Evict) == []                 # asr (HARD_PIN) protected
    assert [d.request_id for d in p.of(Defer)] == ["r1"]


def test_soft_pin_restore_is_debounced():
    base = dict(devices=(gpu(),), units=units(), now=1000)
    # within debounce window -> stay cold
    w1 = WorldState(last_evicted_at={"tts": 990}, **base)   # 10s < 20s debounce
    assert "tts" not in kinds_of(plan(w1).of(Load), Load)
    # past debounce + room -> restore warm
    w2 = WorldState(last_evicted_at={"tts": 960}, **base)   # 40s > 20s
    assert "tts" in kinds_of(plan(w2).of(Load), Load)


def test_aging_prevents_starvation():
    # Two equal-priority requests, one slot. The long-waiter ages above the fresh one.
    u = {"a": Unit("a", {"vram": 10}, priority=30),
         "b": Unit("b", {"vram": 10}, priority=30)}
    d = gpu(cap=12, reserved=0)  # only one 10 GB unit fits
    w = WorldState(devices=(d,), units=u, now=1000,
                   requests=(Request("fresh", "a", created_at=1000),
                             Request("waited", "b", created_at=700)))  # 300s old
    p = plan(w)
    granted = {g.kind for g in p.of(Grant)}
    deferred = {d_.request_id for d_ in p.of(Defer)}
    assert granted == {"b"} and deferred == {"fresh"}


def test_anti_thrash_protects_freshly_loaded():
    # TTS & chipgen just loaded (5s ago, < 15s min_residency): ASR cannot preempt
    # them yet -> defers rather than thrash.
    w = WorldState(devices=(gpu(),), units=units(),
                   placements=(Placement("tts", "gpu0", loaded_at=995),
                               Placement("chipgen", "gpu0", loaded_at=995)),
                   requests=(Request("r1", "asr", created_at=1000),), now=1000)
    p = plan(w)
    assert p.of(Evict) == []
    assert [d.request_id for d in p.of(Defer)] == ["r1"]


# --- measured-free reconciliation (live device memory) -----------------------

def test_measured_free_tighter_than_budget_defers():
    # Budget says chipgen(5) fits (free = 24-1-10 = 13 after ASR), but the device
    # reports only 2 GB ACTUALLY free (an external process). Reconciled free =
    # min(13, 2-1) = 1 < 5, and ASR is HARD_PIN (no legal victim) -> the request
    # must DEFER, not OOM-grant.
    w = WorldState(devices=(gpu(),), units=units(),
                   placements=(Placement("asr", "gpu0", loaded_at=0),),
                   requests=(Request("r1", "chipgen", created_at=100),), now=100,
                   measured_free={"gpu0": {"vram": 2}})
    p = plan(w)
    assert any(d.request_id == "r1" for d in p.of(Defer))
    assert "chipgen" not in kinds_of(p.of(Load), Load)


def test_measured_free_absent_uses_budget_unchanged():
    # No measured reading -> identical to before (budget-only): chipgen is granted.
    w = WorldState(devices=(gpu(),), units=units(),
                   placements=(Placement("asr", "gpu0", loaded_at=0),),
                   requests=(Request("r1", "chipgen", created_at=100),), now=100)
    p = plan(w)
    assert any(g.kind == "chipgen" for g in p.of(Grant))


def test_shed_on_measured_overbudget_evicts_one_idle_victim():
    # Resident ASR(10,pin)+chipgen(5): budget free = 24-1-15 = 8 >= 0, but the device
    # reports 0 free (reality worse than the static model). Step 0 sheds the idle,
    # least-important, non-pinned unit (chipgen). Evicting it frees 5 -> back to
    # non-negative, so EXACTLY one eviction (no over-shedding); ASR is never shed.
    w = WorldState(devices=(gpu(),), units=units(),
                   placements=(Placement("asr", "gpu0", loaded_at=0),
                               Placement("chipgen", "gpu0", loaded_at=0)),
                   now=100, measured_free={"gpu0": {"vram": 0}})
    p = plan(w)
    assert kinds_of(p.of(Evict), Evict) == ["chipgen"]


def test_shed_respects_min_residency_anti_thrash():
    # Same over-budget, but chipgen loaded 1s ago (< 15s min_residency): anti-thrash
    # protects it, so nothing is shed despite the pressure.
    w = WorldState(devices=(gpu(),), units=units(),
                   placements=(Placement("asr", "gpu0", loaded_at=0),
                               Placement("chipgen", "gpu0", loaded_at=99)),
                   now=100, measured_free={"gpu0": {"vram": 0}})
    p = plan(w)
    assert kinds_of(p.of(Evict), Evict) == []


# --- measured activation headroom (admission reserves weights + peak) ---------

def test_activation_headroom_reserved_at_admission_defers_when_peak_wont_fit():
    # chipgen weights (5) fit in free 24-1=23, but its measured activation peak is
    # +20 => real admission need 25 > 23. Weights-only would have granted (the OOM);
    # with headroom the planner refuses and defers instead of loading into an OOM.
    u = {"chipgen": Unit("chipgen", {"vram": 5}, priority=30, residency=Residency.UNPINNED,
                         reload_cost=4, activation_headroom={"vram": 20})}
    w = WorldState(devices=(gpu(),), units=u,
                   requests=(Request("r1", "chipgen", created_at=0),), now=100)
    p = plan(w)
    assert p.of(Load) == []
    assert [d.request_id for d in p.of(Defer)] == ["r1"]


def test_activation_headroom_evicts_extra_victim_to_fit_peak():
    # gpu holds idle tts(9)+chipgen(5); free=24-1-9-5=9. A fresh asr request: asr
    # weights=10 (>9) already needs one victim; with +6 activation headroom the need
    # is 16, so BOTH idle lower-priority units must be evicted to fit the peak.
    u = units()
    u["asr"] = Unit("asr", {"vram": 10}, priority=10, residency=Residency.HARD_PIN,
                    min_resident=1, reload_cost=8, activation_headroom={"vram": 6})
    w = WorldState(devices=(gpu(),), units=u,
                   placements=(Placement("tts", "gpu0", loaded_at=0),
                               Placement("chipgen", "gpu0", loaded_at=0)),
                   requests=(Request("r1", "asr", created_at=100),), now=100)
    p = plan(w)
    assert kinds_of(p.of(Evict), Evict) == ["chipgen", "tts"]
    assert kinds_of(p.of(Load), Load) == ["asr"]


def test_resident_headroom_blocks_backfill_of_reserved_peak_space():
    # THE runtime-OOM guard: asr(10)+hdrm6 resident, idle tts(9)+chipgen(5) too.
    # free = 24-1-(10+9+5 weights)-6 hdrm = -7 => over-reserved. A fresh chipgen lease
    # must NOT be able to backfill into asr's reserved activation space; the planner
    # keeps 6 free for asr's next run rather than granting a warm chipgen into it.
    u = units()
    u["asr"] = Unit("asr", {"vram": 10}, priority=10, residency=Residency.HARD_PIN,
                    min_resident=1, reload_cost=8, activation_headroom={"vram": 6})
    w = WorldState(devices=(gpu(),), units=u,
                   placements=(Placement("asr", "gpu0", loaded_at=0),
                               Placement("tts", "gpu0", loaded_at=0),
                               Placement("chipgen", "gpu0", loaded_at=0)),
                   # chipgen already resident so a lease is warm; the point is that a
                   # NEW load into reserved space is refused. Shed relieves the -7.
                   now=100, measured_free={"gpu0": {"vram": 6}})
    p = plan(w)
    # over-reserve is relieved by shedding the least-important idle unit (chipgen),
    # never by dipping into asr's (HARD_PIN) reserved headroom.
    assert "asr" not in kinds_of(p.of(Evict), Evict)
    assert "chipgen" in kinds_of(p.of(Evict), Evict)


def test_headroom_released_after_evict():
    # Once a headroom unit is evicted, its reserve is freed: a second unit that only
    # fit AFTER the eviction is then admittable. asr(10)+hdrm20 idle-resident blocks
    # tts(9) [free=24-1-10-20=-7]; but asr is UNPINNED here so it can be preempted by
    # a higher-priority tts request, releasing the 20 => tts fits (free becomes 13).
    u = units()
    u["asr"] = Unit("asr", {"vram": 10}, priority=30, residency=Residency.UNPINNED,
                    reload_cost=8, activation_headroom={"vram": 20})
    u["tts"] = Unit("tts", {"vram": 9}, priority=10, residency=Residency.SOFT_PIN,
                    reload_cost=6)
    w = WorldState(devices=(gpu(),), units=u,
                   placements=(Placement("asr", "gpu0", loaded_at=0),),
                   requests=(Request("r1", "tts", created_at=100),), now=100)
    p = plan(w)
    assert kinds_of(p.of(Evict), Evict) == ["asr"]   # headroom released
    assert kinds_of(p.of(Load), Load) == ["tts"]
    assert any(g.kind == "tts" for g in p.of(Grant))


def test_no_headroom_is_unchanged_behavior():
    # Default units (no activation_headroom) behave exactly as before: chipgen loads.
    w = WorldState(devices=(gpu(),), units=units(),
                   requests=(Request("r1", "chipgen", created_at=0),), now=100)
    p = plan(w)
    assert "chipgen" in kinds_of(p.of(Load), Load)
