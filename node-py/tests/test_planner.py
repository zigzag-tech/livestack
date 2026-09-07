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
def test_measured_free_forces_request_driven_eviction_of_unpinned():
    # The real tower0 case: a HARD_PIN (asr) and an idle UNPINNED (chipgen) are
    # resident, and the declared footprints UNDERSTATE reality — the static budget
    # (24-1-(10+5)=8) looks like a mid-priority align(fp5) fits with room to spare.
    # But the device reports only 3 GB ACTUALLY free. The reconciled free =
    # min(8, 3-1) = 2 < 5, so the planner must EVICT the idle UNPINNED chipgen
    # (never the HARD_PIN asr) to admit align — instead of granting into phantom
    # footprint free and OOMing. This is the defect's regression guard.
    u = units()
    u["align"] = Unit("align", {"vram": 5}, priority=15, residency=Residency.UNPINNED,
                      reload_cost=5)
    w = WorldState(devices=(gpu(),), units=u,
                   placements=(Placement("asr", "gpu0", loaded_at=0),
                               Placement("chipgen", "gpu0", loaded_at=0)),
                   requests=(Request("r1", "align", created_at=100),), now=100,
                   measured_free={"gpu0": {"vram": 3}})
    p = plan(w)
    assert kinds_of(p.of(Evict), Evict) == ["chipgen"]      # idle UNPINNED shed
    assert "asr" not in kinds_of(p.of(Evict), Evict)        # HARD_PIN never evicted
    assert any(g.kind == "align" and g.device_id == "gpu0" for g in p.of(Grant))


# --- hosted backends --------------------------------------------------------
#
# A hosted backend is somebody else's GPU behind an API (Qwen ASR). It has no
# residency to arbitrate: nothing loads, nothing evicts, nothing idles out. What
# it has is a concurrency ceiling, a price, and an uptime — so it is scheduled by
# lease count, ranked by `cost_bias`, and gated by `available`.

def hosted(id="qwen-sg", concurrency=None, bias=0.0, available=True, labels=None):
    return Device(id=id, host_id="hosted",
                  capacity={"concurrency": concurrency} if concurrency else {},
                  labels=labels or {}, hosted=True,
                  cost_bias=bias, available=available)


def test_hosted_default_wins_over_a_warm_local_replica():
    """A negative bias is what "the API is the default" means: it beats even a
    resident local copy, whose cost is 0. This is the flip that frees the GPU."""
    w = WorldState(
        devices=(gpu(), hosted(bias=-1.0)),
        units=units(),
        placements=(Placement(kind="asr", device_id="gpu0", leases=0),),
        requests=(Request(id="r1", kind="asr"),),
    )
    p = plan(w)
    assert [g.device_id for g in p.of(Grant)] == ["qwen-sg"]
    # Nothing is loaded or evicted FOR THIS GRANT — a hosted device has no
    # residency. (Unrelated pin/restore traffic for other units may still occur.)
    assert [a for a in p.of(Load) if a.device_id == "qwen-sg"] == []
    assert [a for a in p.of(Evict) if a.device_id == "qwen-sg"] == []


def test_hosted_as_overflow_only_when_bias_is_positive():
    """The same knob the other way: the GPU is preferred, and the API absorbs
    what would otherwise force a load or a preemption."""
    warm = WorldState(
        devices=(gpu(), hosted(bias=5.0)),
        units=units(),
        placements=(Placement(kind="asr", device_id="gpu0"),),
        requests=(Request(id="r1", kind="asr"),),
    )
    assert [g.device_id for g in plan(warm).of(Grant)] == ["gpu0"]

    # Now the card is full of busier, more important work that cannot be
    # preempted, so a local grant is infeasible. Overflow goes hosted instead —
    # which beats the old behaviour, where the request was simply deferred.
    full = WorldState(
        devices=(gpu(cap=12, reserved=1), hosted(bias=5.0)),
        units=units(),
        placements=(Placement(kind="asr", device_id="gpu0", busy=True, leases=1),),
        requests=(Request(id="r1", kind="chipgen"),),
    )
    p = plan(full)
    assert [g.device_id for g in p.of(Grant)] == ["qwen-sg"]
    assert p.of(Defer) == [], "overflow should absorb it rather than defer"


def test_an_unavailable_hosted_backend_falls_back_to_the_gpu():
    """A rate-limited or down endpoint is simply not a candidate, so demand
    lands locally with no special case anywhere else."""
    w = WorldState(
        devices=(gpu(), hosted(bias=-1.0, available=False)),
        units=units(),
        requests=(Request(id="r1", kind="asr"),),
    )
    p = plan(w)
    assert [g.device_id for g in p.of(Grant)] == ["gpu0"]
    assert "asr" in [l.kind for l in p.of(Load)], "it must load locally instead"


def test_hosted_concurrency_is_the_ceiling_leases_not_bytes():
    """Residence is free there; in-flight requests are not."""
    at_cap = WorldState(
        devices=(gpu(), hosted(concurrency=2, bias=-1.0)),
        units=units(),
        placements=(Placement(kind="asr", device_id="qwen-sg", leases=2),),
        requests=(Request(id="r1", kind="asr"),),
    )
    assert [g.device_id for g in plan(at_cap).of(Grant)] == ["gpu0"], \
        "a full hosted backend must not absorb another lease"

    under_cap = WorldState(
        devices=(gpu(), hosted(concurrency=2, bias=-1.0)),
        units=units(),
        placements=(Placement(kind="asr", device_id="qwen-sg", leases=1),),
        requests=(Request(id="r1", kind="asr"),),
    )
    assert [g.device_id for g in plan(under_cap).of(Grant)] == ["qwen-sg"]


def test_a_pin_floor_is_never_satisfied_by_a_hosted_backend():
    """HARD_PIN means "keep a warm local replica". A hosted device holds nothing,
    so letting it satisfy the floor would report a guarantee we do not have."""
    w = WorldState(
        devices=(gpu(), hosted(bias=-1.0)),
        units=units(),
    )
    loads = plan(w).of(Load)
    assert [l.device_id for l in loads if l.kind == "asr"] == ["gpu0"]


def test_hosted_devices_are_exempt_from_measured_pressure_shedding():
    """Over-budget shedding reclaims bytes. A hosted backend has none, and its
    units are not evictable — a negative reading there must not evict anything."""
    w = WorldState(
        devices=(hosted(bias=-1.0), gpu()),
        units=units(),
        placements=(Placement(kind="asr", device_id="qwen-sg", leases=1),),
        measured_free={"qwen-sg": {"vram_bytes": -99.0}},
    )
    assert plan(w).of(Evict) == []


# --- packing and residency as a consequence of the QUEUE ---------------------
#
# What these pin down is that nothing anywhere says "one LLM per GPU". That
# arrangement is what the planner arrives at when the workload alternates
# between two models, and it stops paying for it when the workload stops.
#
# The alternative they rule out is pinning each service to a card in its unit
# file (CUDA_VISIBLE_DEVICES): a decision made outside the planner, which
# therefore cannot adapt when a card fills or frees.

def _llms(small=8, big=20, group="llm"):
    """A small model and one that needs most of a card."""
    return {
        "llm_small": Unit("llm_small", {"vram": small}, priority=20,
                          reload_cost=30, spread_group=group),
        "llm_big": Unit("llm_big", {"vram": big}, priority=20,
                        reload_cost=60, spread_group=group),
    }


def test_alternating_demand_settles_one_llm_per_card():
    """The queue holds work for both models; they cannot share a card. The
    planner separates them because co-residence would thrash — not because it
    was told to."""
    w = WorldState(devices=(gpu("gpu0"), gpu("gpu1")), units=_llms(),
                   placements=(Placement("llm_small", "gpu0", loaded_at=0),),
                   requests=(Request("r1", "llm_big", created_at=0),),
                   demand={"llm_small": 40, "llm_big": 40}, now=1000)
    p = plan(w)
    grants = [g for g in p.of(Grant) if g.kind == "llm_big"]
    assert grants and grants[0].device_id == "gpu1", (
        "with both models in demand, the big one takes the free card instead of "
        "evicting the small one")
    assert not [e for e in p.of(Evict) if e.kind == "llm_small"]


def test_no_demand_for_the_sibling_means_no_reason_to_avoid_it():
    """Same shape, but nothing is asking for the small model. Keeping the cards
    apart buys nothing, so the planner is free to preempt it — residency follows
    the workload."""
    w = WorldState(devices=(gpu("gpu0"),), units=_llms(),
                   placements=(Placement("llm_small", "gpu0", loaded_at=0),),
                   requests=(Request("r1", "llm_big", created_at=0),),
                   demand={"llm_small": 0, "llm_big": 50}, now=1000)
    p = plan(w)
    assert [g for g in p.of(Grant) if g.kind == "llm_big"]
    assert "llm_small" in kinds_of(p.of(Evict), Evict)


def test_a_small_unit_takes_the_tighter_card_and_leaves_a_whole_one_free():
    """Best fit, not first fit. gpu0 is empty; gpu1 already has a tenant and
    still has room. The small model goes to gpu1 — so a model that needs a whole
    card can still be placed later."""
    us = dict(_llms())
    us["other"] = Unit("other", {"vram": 6}, priority=30, reload_cost=5)
    w = WorldState(devices=(gpu("gpu0"), gpu("gpu1")), units=us,
                   placements=(Placement("other", "gpu1", loaded_at=0),),
                   requests=(Request("r1", "llm_small", created_at=0),),
                   demand={}, now=1000)
    loads = [l for l in plan(w).of(Load) if l.kind == "llm_small"]
    assert loads and loads[0].device_id == "gpu1", (
        "an 8 GB unit should pack beside the 6 GB tenant, not consume the empty card")


def test_a_unit_in_the_way_is_MOVED_not_dropped():
    """The Tetris case: the small model is sitting on the only card big enough
    for the 20 GB model, and the other card has room. It steps aside and keeps
    serving, instead of being evicted until a debounce brings it back."""
    w = WorldState(
        # gpu0 (24 GB) is the ONLY card the 20 GB model can ever fit on, and the
        # small one is sitting on it. gpu1 (14 GB) has room for the small one.
        devices=(gpu("gpu0", cap=24), gpu("gpu1", cap=14)), units=_llms(),
        placements=(Placement("llm_small", "gpu0", loaded_at=0),),
        requests=(Request("r1", "llm_big", created_at=0),),
        demand={"llm_small": 30, "llm_big": 30}, now=1000)
    p = plan(w)
    big = [g for g in p.of(Grant) if g.kind == "llm_big"]
    assert big and big[0].device_id == "gpu0"
    assert [e for e in p.of(Evict) if e.kind == "llm_small" and e.device_id == "gpu0"]
    reload = [l for l in p.of(Load) if l.kind == "llm_small"]
    assert reload and reload[0].device_id == "gpu1", (
        "the displaced unit must be relocated to the card with room, not dropped")
    assert "relocated" in reload[0].reason


def test_nothing_is_relocated_when_there_is_nowhere_to_go():
    """One card. Preemption still means eviction — relocation is an improvement
    on the victim's fate, never a requirement for the placement to happen."""
    w = WorldState(devices=(gpu("gpu0"),), units=_llms(),
                   placements=(Placement("llm_small", "gpu0", loaded_at=0),),
                   requests=(Request("r1", "llm_big", created_at=0),),
                   demand={"llm_big": 10}, now=1000)
    p = plan(w)
    assert "llm_small" in kinds_of(p.of(Evict), Evict)
    assert not [l for l in p.of(Load) if l.kind == "llm_small"]


def test_demand_is_absent_by_default_and_changes_nothing():
    """Every existing WorldState omits `demand`; the term is then zero and the
    planner behaves exactly as it did before it existed."""
    w = WorldState(devices=(gpu(),), units=units(),
                   requests=(Request("r1", "chipgen", created_at=0),), now=100)
    assert any(g.kind == "chipgen" for g in plan(w).of(Grant))


def test_an_unknown_kind_is_deferred_not_a_crash():
    """A request for a unit nobody registered must come back as one deferred
    request, not take the whole plan down.

    It used to raise KeyError from the sort key — before the loop that handles
    exactly this case. The broker above then reported its own exception as
    `granted: True`, and a node acting on that permission loaded a model onto a
    card the planner had never cleared."""
    w = WorldState(devices=(gpu(),), units=units(),
                   requests=(Request("r1", "no_such_unit", created_at=0),
                             Request("r2", "chipgen", created_at=0)), now=100)
    p = plan(w)                                     # must not raise
    deferred = [d for d in p.of(Defer) if d.request_id == "r1"]
    assert deferred and "unknown" in deferred[0].reason
    # The rest of the plan still happens: one bad request is not a broken cycle.
    assert any(g.kind == "chipgen" for g in p.of(Grant))


# --- equal priority, and one of them is wanted --------------------------------

def _two_llms(demand=None):
    """Two 15 GB LLMs, SAME priority, one 24 GB card. Only one fits.

    `created_at == now` on purpose: anti-starvation aging would otherwise lift
    the requester's effective priority well past the resident's (20 -> -60 after
    1000s), and the ordinary lower-priority preemption path would fire instead
    of the equal-priority rule these tests are about."""
    us = {
        "llm_a": Unit("llm_a", {"vram": 15}, priority=20, residency=Residency.UNPINNED,
                      reload_cost=30, spread_group="llm", min_residency_s=0),
        "llm_b": Unit("llm_b", {"vram": 15}, priority=20, residency=Residency.UNPINNED,
                      reload_cost=30, spread_group="llm", min_residency_s=0),
    }
    return WorldState(devices=(gpu("gpu0"),), units=us,
                      placements=(Placement("llm_a", "gpu0", loaded_at=0),),
                      requests=(Request("r1", "llm_b", created_at=1000),),
                      demand=demand or {}, now=1000)


def test_an_idle_unwanted_peer_yields_the_card():
    """Nobody is asking for llm_a; llm_b is. Equal priority must not deadlock the
    card — otherwise the only way through is an external evict, and that is a
    race: anything can re-warm the evicted unit before the new one is placed."""
    p = plan(_two_llms(demand={"llm_b": 12}))
    assert "llm_a" in kinds_of(p.of(Evict), Evict)
    assert [g for g in p.of(Grant) if g.kind == "llm_b" and g.device_id == "gpu0"]


def test_a_busy_peer_never_yields_even_when_unwanted():
    """`demand` is about queued work, `busy` is about work in flight. A unit
    serving a request is not a victim no matter what the tally says."""
    us = {
        "llm_a": Unit("llm_a", {"vram": 15}, priority=20, residency=Residency.UNPINNED,
                      reload_cost=30, min_residency_s=0),
        "llm_b": Unit("llm_b", {"vram": 15}, priority=20, residency=Residency.UNPINNED,
                      reload_cost=30, min_residency_s=0),
    }
    w = WorldState(devices=(gpu("gpu0"),), units=us,
                   placements=(Placement("llm_a", "gpu0", loaded_at=0, busy=True),),
                   requests=(Request("r1", "llm_b", created_at=1000),),
                   demand={"llm_b": 5}, now=1000)
    assert "llm_a" not in kinds_of(plan(w).of(Evict), Evict)


def test_a_pinned_peer_does_not_yield_at_equal_priority():
    """Only UNPINNED yields on the equal-priority path. SOFT_PIN says 'keep me
    warm unless someone MORE important needs the room'."""
    us = {
        "llm_a": Unit("llm_a", {"vram": 15}, priority=20, residency=Residency.SOFT_PIN,
                      reload_cost=30, min_residency_s=0),
        "llm_b": Unit("llm_b", {"vram": 15}, priority=20, residency=Residency.UNPINNED,
                      reload_cost=30, min_residency_s=0),
    }
    w = WorldState(devices=(gpu("gpu0"),), units=us,
                   placements=(Placement("llm_a", "gpu0", loaded_at=0),),
                   requests=(Request("r1", "llm_b", created_at=1000),),
                   demand={"llm_b": 5}, now=1000)
    assert "llm_a" not in kinds_of(plan(w).of(Evict), Evict)


def test_the_card_goes_to_whoever_wants_it_more():
    """Demand decays continuously, so a strict "resident must be at zero" test
    leaves a long tail where a model finished with long ago still holds a card
    against one being actively requested. Measured: 0.47 vs a live requester,
    22 minutes after the last call. The comparison is relative."""
    w = _two_llms(demand={"llm_a": 0.47, "llm_b": 3.0})
    p = plan(w)
    assert "llm_a" in kinds_of(p.of(Evict), Evict)
    assert [g for g in p.of(Grant) if g.kind == "llm_b"]


def test_the_more_wanted_resident_keeps_the_card():
    """...and the comparison runs the other way too."""
    w = _two_llms(demand={"llm_a": 9.0, "llm_b": 1.0})
    p = plan(w)
    assert "llm_a" not in kinds_of(p.of(Evict), Evict)
    assert [d for d in p.of(Defer) if d.request_id == "r1"]


# --- "just give me a model that can do this" ---------------------------------
#
# A caller sending an inference request should not have to know which model is
# loaded, on which card, or whether anything must move to make room. It states
# what it needs; the planner resolves it. These pin that contract.

def _catalogue():
    return {
        "llm_small": Unit("llm_small", {"vram": 8}, priority=20, residency=Residency.UNPINNED,
                          reload_cost=20, spread_group="llm", min_residency_s=0,
                          attributes={"class": "llm", "params_b": 9, "quant": "int4"}),
        "llm_big": Unit("llm_big", {"vram": 20}, priority=20, residency=Residency.UNPINNED,
                        reload_cost=60, spread_group="llm", min_residency_s=0,
                        attributes={"class": "llm", "params_b": 27, "quant": "int4"}),
        "asr": Unit("asr", {"vram": 5}, priority=10, residency=Residency.UNPINNED,
                    reload_cost=8, attributes={"class": "asr"}),
    }


def test_a_requirement_picks_a_qualifying_model_without_naming_one():
    w = WorldState(devices=(gpu("gpu0"),), units=_catalogue(),
                   requests=(Request("r1", "", created_at=1000,
                                     requires={"class": "llm", "params_b>=": 20}),),
                   now=1000)
    g = [x for x in plan(w).of(Grant) if x.request_id == "r1"]
    assert g and g[0].kind == "llm_big", "only the 27B satisfies >= 20B"


def test_a_requirement_prefers_what_is_already_resident():
    """The cheapest outcome first: a qualifying model already on a card is used
    rather than loading another beside it."""
    w = WorldState(devices=(gpu("gpu0"),), units=_catalogue(),
                   placements=(Placement("llm_small", "gpu0", loaded_at=0),),
                   requests=(Request("r1", "", created_at=1000,
                                     requires={"class": "llm"}),),
                   now=1000)
    p = plan(w)
    g = [x for x in p.of(Grant) if x.request_id == "r1"]
    assert g and g[0].kind == "llm_small"
    assert not [l for l in p.of(Load) if l.kind == "llm_big"]


def test_a_requirement_nothing_satisfies_says_so():
    w = WorldState(devices=(gpu("gpu0"),), units=_catalogue(),
                   requests=(Request("r1", "", created_at=1000,
                                     requires={"class": "llm", "params_b>=": 400}),),
                   now=1000)
    d = [x for x in plan(w).of(Defer) if x.request_id == "r1"]
    assert d and "no unit satisfies" in d[0].reason


def test_an_undeclared_attribute_is_not_a_match():
    """Silence is not a yes — an unlabelled unit must not satisfy everything."""
    w = WorldState(devices=(gpu("gpu0"),), units=_catalogue(),
                   requests=(Request("r1", "", created_at=1000,
                                     requires={"class": "llm", "vision": True}),),
                   now=1000)
    assert [x for x in plan(w).of(Defer) if x.request_id == "r1"]


def test_naming_a_kind_still_means_that_kind():
    w = WorldState(devices=(gpu("gpu0"),), units=_catalogue(),
                   requests=(Request("r1", "llm_small", created_at=1000),), now=1000)
    g = [x for x in plan(w).of(Grant) if x.request_id == "r1"]
    assert g and g[0].kind == "llm_small"
