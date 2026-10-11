"""No-effect qualification planning; caller still owns authorization and leases.

The normal dispatcher may warm and evict unrelated models. This lane only
inspects its plan, and refuses any such action before a caller can dispatch it.
"""
import math
from dataclasses import replace
from .planner import Defer, Evict, Grant, Load, Request, plan


def plan_qualification_capacity(world, unit, device_id, request_id, owner):
    devices = [device for device in world.devices if device.id == device_id]
    if len(devices) != 1 or device_id not in unit.servable_on:
        raise ValueError('qualification_exact_serving_device_required')
    footprint = unit.admission_footprint or unit.footprint
    measured = world.measured_free.get(device_id)
    if not footprint or not measured:
        raise ValueError('qualification_measured_capacity_required')
    for key, amount in footprint.items():
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(amount) or amount <= 0:
            raise ValueError('qualification_positive_footprint_required')
        # Host resources are accounted by the canonical planner, never inferred
        # from GPU capacity. Unmeasured dimensions must still refuse there.
        if key in devices[0].capacity:
            free = measured.get(key)
            if isinstance(free, bool) or not isinstance(free, (int, float)) or not math.isfinite(free) or free < amount:
                raise ValueError('qualification_spare_capacity_unproven')
    if unit.kind in world.units and world.units[unit.kind] != unit:
        raise ValueError('qualification_ambiguous_unit_identity')
    selected = replace(unit, servable_on=frozenset({device_id}))
    request = Request(id=request_id, kind=unit.kind, owner=owner, created_at=world.now)
    candidate = replace(world, units={**world.units, unit.kind: selected}, requests=(request,))
    result = plan(candidate)
    for action in result.actions:
        if isinstance(action, Evict) or isinstance(action, Load) and (action.kind != unit.kind or action.device_id != device_id):
            raise ValueError('qualification_would_change_unrelated_residency')
        if isinstance(action, Grant) and (action.request_id != request_id or action.kind != unit.kind or action.device_id != device_id):
            raise ValueError('qualification_wrong_grant')
    grants = [action for action in result.actions if isinstance(action, Grant)]
    if len(grants) != 1 or any(isinstance(action, Defer) for action in result.actions):
        raise ValueError('qualification_capacity_refused')
    return result
