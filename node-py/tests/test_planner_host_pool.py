"""Host RAM is a HOST-scoped planned resource, not a per-card one.

Two units that each need 45 GB of pinned host RAM fit two 24 GB cards
separately and together swap the machine, so `ram_bytes` is fitted against the
host pool (`WorldState.hosts`) shared by every device on that host. A host with
no measurement is UNMEASURED, which is not "empty": a unit carrying `ram_bytes`
is refused `host memory unmeasured` and units without one place as before.

Positive control: the two-units test below FAILS against the pre-host-pool
planner (WorldState has no `hosts`, and the second unit places).
"""
from livestack_node.planner import (
    Defer, Device, Evict, Grant, Load, Placement, Request, Residency, Unit,
    WorldState, plan,
)

GB = 1.0


def gpu(id, host, cap=24.0):
    return Device(id=id, host_id=host,
                  capacity={"vram_bytes": cap}, reserved={"vram_bytes": 0.0})


def ram_unit(kind, vram=20.0, ram=45.0, priority=100, servable=()):
    return Unit(kind, {"vram_bytes": vram, "ram_bytes": ram},
                priority=priority, residency=Residency.UNPINNED,
                servable_on=frozenset(servable), reload_cost=50.0)


def test_two_ram_heavy_units_on_one_host_cannot_both_be_placed():
    units = {
        # ram_b is less important (higher priority int), so the refusal is about
        # the HOST POOL and not about the anti-thrash floor standing between it
        # and an evictable sibling.
        "ram_a": ram_unit("ram_a", priority=100, servable=("gpu0",)),
        "ram_b": ram_unit("ram_b", priority=150, servable=("gpu1",)),
    }
    w = WorldState(
        devices=(gpu("gpu0", "h1"), gpu("gpu1", "h1")), units=units,
        placements=(), now=100.0, hosts={"h1": {"ram_bytes": 64.0 * GB}},
        host_reserve={"h1": {"ram_bytes": 2.0 * GB}},
        requests=(Request("r1", "ram_a", created_at=100.0),
                  Request("r2", "ram_b", created_at=100.0)))
    p = plan(w)
    loaded = [a.kind for a in p.of(Load)]
    assert loaded == ["ram_a"]                     # exactly one gets the RAM
    deferred = {d.request_id: d.reason for d in p.of(Defer)}
    assert "r2" in deferred
    # The refusal names the host pool arithmetic (need / free / reserve).
    assert "host memory" in deferred["r2"]
    assert "45" in deferred["r2"] and "free" in deferred["r2"]


def test_evicting_a_ram_heavy_unit_returns_its_ram_to_the_pool():
    # ram_a holds 45 GB of the host's 64; the measured free already reflects it
    # (19 GB left). A more important ram_b may take the card — and the RAM —
    # from it in one plan.
    units = {
        "ram_a": ram_unit("ram_a", priority=150, servable=("gpu0",)),
        "ram_b": ram_unit("ram_b", priority=100, servable=("gpu0",)),
    }
    w = WorldState(
        devices=(gpu("gpu0", "h1"),), units=units,
        placements=(Placement("ram_a", "gpu0", loaded_at=0.0),), now=100.0,
        hosts={"h1": {"ram_bytes": 19.0 * GB}},
        host_reserve={"h1": {"ram_bytes": 2.0 * GB}},
        requests=(Request("r1", "ram_b", created_at=100.0),))
    p = plan(w)
    assert [a.kind for a in p.of(Evict)] == ["ram_a"]
    assert [a.kind for a in p.of(Load)] == ["ram_b"]
    grant = p.of(Grant)[0]
    # The record carries the arithmetic: need 45, free AFTER the swap 19
    # (the evicted 45 came back and the new 45 went out again), reserve 2.
    assert grant.host_pool["need"] == {"ram_bytes": 45.0 * GB}
    assert grant.host_pool["free"] == {"ram_bytes": 19.0 * GB}
    assert grant.host_pool["reserve"] == {"ram_bytes": 2.0 * GB}


def test_unmeasured_host_refuses_a_ram_heavy_unit_by_name():
    units = {
        "ram_a": ram_unit("ram_a", servable=("gpu0",)),
        "plain": Unit("plain", {"vram_bytes": 5.0}, priority=100,
                      residency=Residency.UNPINNED,
                      servable_on=frozenset(("gpu0",)), reload_cost=10.0),
    }
    w = WorldState(
        devices=(gpu("gpu0", "h1"),), units=units, placements=(), now=100.0,
        requests=(Request("r1", "ram_a", created_at=100.0),
                  Request("r2", "plain", created_at=100.0)))
    p = plan(w)
    deferred = {d.request_id: d.reason for d in p.of(Defer)}
    assert deferred.get("r1") == "host memory unmeasured"
    # Units without a host-scoped footprint place exactly as before.
    assert [a.kind for a in p.of(Load)] == ["plain"]


def test_units_without_ram_bytes_place_exactly_as_before():
    # No `hosts` at all: a plain unit's plan is unchanged (this is every unit
    # the fleet runs today).
    units = {"plain": Unit("plain", {"vram_bytes": 5.0}, priority=100,
                           residency=Residency.UNPINNED, reload_cost=10.0)}
    w = WorldState(devices=(gpu("gpu0", "h1"),), units=units, placements=(),
                   now=100.0, requests=(Request("r1", "plain", created_at=100.0),))
    p = plan(w)
    assert [a.kind for a in p.of(Load)] == ["plain"]
    grant = p.of(Grant)[0]
    assert grant.host_pool == {}                   # no host dims, no record
    # Budget is the device's free as of AFTER the load was committed
    # (24 - the 5 just placed) — unchanged from the pre-host-pool planner.
    assert grant.budget["vram_bytes"] == 19.0


def test_a_ram_heavy_unit_fits_against_the_pool_not_just_its_card():
    # 45 GB of RAM against a 19 GB pool: the card would take it, the host will
    # not. Same world as the eviction test but nothing to evict.
    units = {"ram_a": ram_unit("ram_a", servable=("gpu0",))}
    w = WorldState(
        devices=(gpu("gpu0", "h1"),), units=units, placements=(), now=100.0,
        hosts={"h1": {"ram_bytes": 19.0 * GB}},
        requests=(Request("r1", "ram_a", created_at=100.0),))
    p = plan(w)
    assert p.of(Load) == []
    assert "insufficient host memory" in p.of(Defer)[0].reason
