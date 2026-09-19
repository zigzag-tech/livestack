"""caused_by: a Load or Evict says WHOSE REQUEST produced it.

The 27B reload thrash of 2026-09-19 was diagnosed from journal timestamps
because the ledger could not say who needed the room. These pin the contract:
an action produced by a request carries that request's owner; a rule-0 shed
carries "pressure"; a pin-floor action names the policy, because no request
was involved and inventing an owner would be worse than none.
"""
from livestack_node.planner import (
    Device, Evict, Grant, Load, Placement, Request, Residency, Unit,
    WorldState, plan,
)

GB = 1_000_000_000


def _dev(dev_id="h/gpu0", vram_gb=48.0):
    return Device(id=dev_id, host_id="h",
                  capacity={"vram_bytes": int(vram_gb * GB)},
                  reserved={"vram_bytes": 2 * GB})


def _world(units, *, placements=(), requests=(), devices=None,
           measured_free=None, now=1000.0):
    return WorldState(
        devices=tuple(devices or (_dev(),)),
        units=units,
        placements=tuple(placements),
        requests=tuple(requests),
        now=now,
        measured_free=measured_free or {},
    )


def test_an_eviction_made_for_request_r_carries_rs_owner():
    """The requirement's scenario, in the small: a request that needs the room
    is named on the eviction AND on the load that replaces it — and the Grant
    carries the owner beside the request id, with the asserted flag."""
    resident = Unit("llm", {"vram_bytes": 35 * GB}, priority=30)
    challenger = Unit("align", {"vram_bytes": 35 * GB}, priority=20)
    req = Request(id="r1", kind="align", owner="attune:acct_a",
                  owner_asserted=True)
    world = _world(
        {"llm": resident, "align": challenger},
        placements=(Placement(kind="llm", device_id="h/gpu0"),),
        requests=(req,))
    p = plan(world)
    ev = next(a for a in p.actions if isinstance(a, Evict))
    assert ev.kind == "llm"
    assert ev.caused_by == "attune:acct_a", "the room was needed for R's owner"
    load = next(a for a in p.actions if isinstance(a, Load))
    assert load.kind == "align"
    assert load.caused_by == "attune:acct_a"
    grant = next(a for a in p.actions if isinstance(a, Grant))
    assert grant.owner == "attune:acct_a"
    assert grant.owner_asserted is True


def test_a_rule0_shed_carries_pressure_not_an_owner():
    """Rule 0 sheds to relieve MEASURED over-budget pressure with no request
    in play. There is no owner to name — attributing the shed to whoever asked
    last would be a lie — so it carries the literal string "pressure"."""
    a = Unit("a", {"vram_bytes": 10 * GB}, priority=30)
    b = Unit("b", {"vram_bytes": 10 * GB}, priority=30)
    world = _world(
        {"a": a, "b": b},
        # Two residents: the "do not empty the device" guard needs a second
        # tenant (or a pending request) before it will shed anything.
        placements=(Placement(kind="a", device_id="h/gpu0"),
                    Placement(kind="b", device_id="h/gpu0")),
        measured_free={"h/gpu0": {"vram_bytes": -3 * GB}},
        requests=(),
    )
    p = plan(world)
    evicts = [a for a in p.actions if isinstance(a, Evict)]
    assert evicts, "measured pressure must shed something"
    assert all(e.caused_by == "pressure" for e in evicts)


def test_a_hard_pin_floor_names_the_policy_not_a_request():
    pin = Unit("asr", {"vram_bytes": 5 * GB}, priority=10,
               residency=Residency.HARD_PIN, min_resident=1)
    world = _world({"asr": pin})
    p = plan(world)
    load = next(a for a in p.actions if isinstance(a, Load))
    assert load.kind == "asr"
    assert load.caused_by == "hard-pin floor"


def test_actions_from_before_the_field_default_to_empty():
    """The field is defaulted, so a hand-built action (a reader replaying an
    old plan, a test fixture) constructs exactly as before."""
    assert Evict("k", "d").caused_by == ""
    assert Load("k", "d").caused_by == ""
    assert Grant("r1", "k", "d").owner == ""
    assert Grant("r1", "k", "d").owner_asserted is False
