"""A node-declared priority outranks the tier-derived default.

Two UNPINNED LLMs, declared priorities 20 and 30: the 30 yields. Priority
is what decides who is the victim when two units need one card, and until
the fields in this slice existed every UNPINNED unit was 30 — declared
economics are how a node says "this one is not".
"""
from livestack_node.planner import (
    Device, Evict, Placement, Request, Residency, Unit, WorldState, plan,
)

GB = 1_000_000_000


def _world(declared_prio_resident, declared_prio_challenger):
    resident = Unit("llm_big", {"vram_bytes": 15 * GB}, priority=declared_prio_resident)
    challenger = Unit("llm_title", {"vram_bytes": 15 * GB},
                      priority=declared_prio_challenger)
    return WorldState(
        devices=(Device(id="h/gpu0", host_id="h",
                        capacity={"vram_bytes": 20 * GB},
                        reserved={"vram_bytes": 2 * GB}),),
        units={"llm_big": resident, "llm_title": challenger},
        placements=(Placement(kind="llm_big", device_id="h/gpu0"),),
        # created_at == now: no aging — the declared priorities alone decide.
        requests=(Request(id="r1", kind="llm_title", owner="attune:acct_a",
                          created_at=2000.0),),
        now=2000.0,     # past any residency floor
    )


def test_the_declared_30_yields_to_the_declared_20():
    p = plan(_world(declared_prio_resident=30, declared_prio_challenger=20))
    evicted = [a.kind for a in p.actions if isinstance(a, Evict)]
    assert evicted == ["llm_big"], "the declared 30 yields; the 20 stays"
    grant = next(a for a in p.actions if a.__class__.__name__ == "Grant")
    assert grant.kind == "llm_title"


def test_the_declared_20_does_not_yield_to_the_declared_30():
    p = plan(_world(declared_prio_resident=20, declared_prio_challenger=30))
    assert [a for a in p.actions if isinstance(a, Evict)] == []
    # The request is deferred: the more important resident cannot be preempted.
    assert any(a.__class__.__name__ == "Defer" for a in p.actions)


def test_equal_declared_priorities_fall_back_to_todays_rules():
    # Two 30s behave exactly as two UNPINNED 30s always have: the challenger
    # (more recent demand) may preempt an idle unwanted resident.
    p = plan(_world(declared_prio_resident=30, declared_prio_challenger=30))
    kinds = [a.kind for a in p.actions if isinstance(a, Evict)]
    assert kinds == ["llm_big"]
