"""A unit may claim a whole device.

`exclusive_device: true` charges the device's ENTIRE capacity, so admission is
about EVICTABILITY rather than size: an idle evictable tenant is evicted (even
a more important one — the claim is on the space, not on the work), a busy one
defers the admission, and a HARD_PIN refuses it by name. Nothing co-places with
an exclusive resident, and its grant is "all of it".
"""
from livestack_node.planner import (
    Defer, Device, Evict, Grant, Load, Placement, Request, Residency, Unit,
    WorldState, plan,
)

GB = 1.0


def gpu(id="gpu0", host="h1", cap=24.0):
    return Device(id=id, host_id=host,
                  capacity={"vram_bytes": cap}, reserved={"vram_bytes": 0.0})


LLM_GENERAL = Unit("llm_general", {"vram_bytes": 21.0}, priority=100,
                   residency=Residency.UNPINNED, reload_cost=50.0,
                   min_residency_s=15.0)

FLASH_NEXT = Unit("flash_next", {"vram_bytes": 20.0, "ram_bytes": 45.0},
                  priority=150, residency=Residency.UNPINNED,
                  exclusive_device=True, reload_cost=120.0,
                  min_residency_s=120.0)


def world_with(*, placements=(), requests=(), units=None, hosts=None):
    return WorldState(
        devices=(gpu(),), units=units or {"llm_general": LLM_GENERAL,
                                          "flash_next": FLASH_NEXT},
        placements=tuple(placements), now=100.0,
        hosts=hosts if hosts is not None else {"h1": {"ram_bytes": 64.0 * GB}},
        host_reserve={"h1": {"ram_bytes": 2.0 * GB}},
        requests=tuple(requests))


def test_exclusive_unit_displaces_an_idle_unpinned_tenant():
    # llm_general is MORE important (priority 100 < 150) and still goes: the
    # claim is on the space, and it is idle and past its min residency.
    w = world_with(
        placements=(Placement("llm_general", "gpu0", loaded_at=0.0),),
        requests=(Request("r1", "flash_next", created_at=100.0),))
    p = plan(w)
    assert [a.kind for a in p.of(Evict)] == ["llm_general"]
    assert [a.kind for a in p.of(Load)] == ["flash_next"]
    grant = p.of(Grant)[0]
    # The grant is the WHOLE DEVICE, and the record carries the host pool.
    assert grant.budget["vram_bytes"] == 24.0
    assert grant.host_pool == {"need": {"ram_bytes": 45.0 * GB},
                               "free": {"ram_bytes": 19.0 * GB},
                               "reserve": {"ram_bytes": 2.0 * GB}}


def test_exclusive_unit_blocked_by_a_hard_pin_names_the_tenant():
    asr = Unit("asr", {"vram_bytes": 8.0}, priority=10,
               residency=Residency.HARD_PIN, min_resident=1)
    w = world_with(units={"asr": asr, "flash_next": FLASH_NEXT},
                   placements=(Placement("asr", "gpu0", loaded_at=0.0),),
                   requests=(Request("r1", "flash_next", created_at=100.0),))
    p = plan(w)
    assert p.of(Load) == []
    reason = {d.request_id: d.reason for d in p.of(Defer)}["r1"]
    assert reason == ("exclusive unit flash_next needs device gpu0 empty; "
                      "asr is HARD_PIN there")


def test_exclusive_unit_waits_while_the_tenant_is_busy():
    w = world_with(
        placements=(Placement("llm_general", "gpu0", loaded_at=0.0,
                              busy=True, leases=1),),
        requests=(Request("r1", "flash_next", created_at=100.0),))
    p = plan(w)
    assert p.of(Evict) == []
    assert p.of(Load) == []
    reason = {d.request_id: d.reason for d in p.of(Defer)}["r1"]
    assert "busy" in reason and "waits" in reason


def test_a_young_tenant_is_protected_by_the_residency_floor():
    w = world_with(
        placements=(Placement("llm_general", "gpu0", loaded_at=95.0),),
        requests=(Request("r1", "flash_next", created_at=100.0),))
    p = plan(w)
    assert p.of(Evict) == []
    reason = {d.request_id: d.reason for d in p.of(Defer)}["r1"]
    assert reason.startswith("residency floor:")


def test_nothing_co_places_with_a_resident_exclusive_unit():
    # flash_next holds the whole card. A small, less-important unit cannot
    # squeeze beside it — the device reads as FULL. (`hosts` already reflects
    # the resident 45 GB pin: 64 - 45 free.)
    small = Unit("small", {"vram_bytes": 5.0}, priority=200,
                 residency=Residency.UNPINNED, reload_cost=5.0)
    w = world_with(units={"flash_next": FLASH_NEXT, "small": small},
                   placements=(Placement("flash_next", "gpu0", loaded_at=0.0),),
                   hosts={"h1": {"ram_bytes": 19.0 * GB}},
                   requests=(Request("r1", "small", created_at=100.0),))
    p = plan(w)
    assert p.of(Load) == []
    assert [d.request_id for d in p.of(Defer)] == ["r1"]


def test_an_exclusive_resident_is_never_read_as_over_budget_pressure():
    # Charged the whole card, its own free reads 0 — and 0 must not shed the
    # tenant that owns it (that was the shed-and-reload loop of 2026-09-18).
    w = world_with(placements=(Placement("flash_next", "gpu0", loaded_at=0.0),),
                   hosts={"h1": {"ram_bytes": 19.0 * GB}},
                   requests=())
    p = plan(w)
    assert p.of(Evict) == []
    assert p.actions == ()


def test_the_whole_device_claim_fits_the_whole_device_not_capacity_minus_slack():
    """The reserve and the activation headroom exist to protect CO-TENANTS;
    an exclusive claim has none left. Fitting against
    capacity - reserve - headroom could never admit a 22 GiB claim on a 24 GB
    card — measured 2026-10-02 as a flat "the planner could not place it on
    any device" against a card whose only tenant was idle and evictable."""
    from livestack_node.planner import Device, Placement, Request, Unit, WorldState, plan
    card = Device(id="gpu0", host_id="h1",
                  capacity={"vram_bytes": 25.3e9},
                  reserved={"vram_bytes": 2.0e9})
    llm = Unit("llm_general", {"vram_bytes": 22.5e9}, priority=100,
               residency=Residency.UNPINNED, reload_cost=50.0,
               min_residency_s=15.0,
               activation_headroom={"vram_bytes": 2.56e9})
    flash = Unit("flash_next", {"vram_bytes": 23.6e9, "ram_bytes": 45.0e9},
                 priority=150, residency=Residency.UNPINNED,
                 exclusive_device=True, reload_cost=120.0, min_residency_s=120.0)
    w = WorldState(devices=(card,),
                   units={"llm_general": llm, "flash_next": flash},
                   placements=(Placement("llm_general", "gpu0", loaded_at=0.0),),
                   now=100.0,
                   hosts={"h1": {"ram_bytes": 64.0e9}},
                   requests=(Request("r1", "flash_next", created_at=100.0),))
    p = plan(w)
    assert [a.kind for a in p.of(Evict)] == ["llm_general"]
    assert [a.kind for a in p.of(Load)] == ["flash_next"]
