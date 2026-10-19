"""A planned Load carries the room the unit may use.

2026-10-19: llm_general was loaded by the plan (hostbroker warm), which read
`Load.budget` -- a field that did not exist -- so the node fell back to its
configured 0.96 of the WHOLE card with 21.2 of 23.6 GiB free and vLLM refused
to start, forever. The budget must ride on the Load, measured after evictions
and before the unit itself is charged.
"""
from livestack_node.planner import (
    Device, Grant, Load, Request, Unit, WorldState, plan,
)

GB = 1_000_000_000


def _world(unit, measured_free=None):
    return WorldState(
        devices=(Device(id="h/gpu0", host_id="h",
                        capacity={"vram_bytes": 24 * GB},
                        reserved={"vram_bytes": 1 * GB}),),
        units={"llm": unit},
        placements=(),
        requests=(Request(id="r1", kind="llm", owner="o"),),
        now=1000.0,
        measured_free=measured_free or {},
    )


def test_load_budget_is_the_free_room_not_the_whole_card():
    p = plan(_world(Unit("llm", {"vram_bytes": 20 * GB}),
                    measured_free={"h/gpu0": {"vram_bytes": 21 * GB}}))
    load = next(a for a in p.actions if isinstance(a, Load))
    assert load.budget["vram_bytes"] == 20 * GB  # 21 measured - 1 reserved
    assert next(a for a in p.actions if isinstance(a, Grant)).device_id == load.device_id


def test_load_budget_of_an_exclusive_unit_is_the_whole_device():
    p = plan(_world(Unit("llm", {"vram_bytes": 20 * GB}, exclusive_device=True)))
    load = next(a for a in p.actions if isinstance(a, Load))
    assert load.budget["vram_bytes"] == 24 * GB
