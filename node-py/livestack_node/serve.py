"""attach() — the one call both polyasr and polytts make, identically, to become
livestack nodes. The server builds its polycore ManagedUnits (with per-unit
ResidencyPolicy for pinning) and supplies a ``gpu_call`` that runs a thunk under
its GPU discipline; everything else is uniform.

    manager, coord = attach(app, host_id="zz-tower0", kind="polyasr",
                            units=units, idle_seconds=180, coload=True,
                            gpu_call=gpu_call)

Only ``coload`` (True for polyasr's co-resident units, False for polytts'
one-model-in-VRAM engines) and which unit is HARD_PIN differ between the two.
"""
from __future__ import annotations

from typing import Callable, Dict

from .coordinator import LivestackCoordinator
from .facade import build_router
from .lease import Capability


def attach(app, *, host_id: str, kind: str, units: Dict[str, object],
           idle_seconds: int, coload: bool, gpu_call: Callable[[Callable], object],
           prefix: str = "/livestack", device_meter="auto"):
    """``device_meter``: a zero-arg callable -> measured {capacity,free} (see
    meters.py), ``"auto"`` to pick one by backend (CUDA/MLX), or ``None`` to report
    no live memory. Defaulting to "auto" means a node becomes memory-aware on
    redeploy with no server-side change."""
    from polycore import ModelManager
    coordinator = LivestackCoordinator(host_id, coload=coload, usage_ttl_seconds=idle_seconds)
    manager = ModelManager(units, idle_seconds, coordinator=coordinator)
    if device_meter == "auto":
        from .meters import auto_meter
        device_meter = auto_meter()
    app.include_router(
        build_router(manager, coordinator, Capability(kind=kind, host_id=host_id),
                     gpu_call, device_meter=device_meter),
        prefix=prefix,
    )
    return manager, coordinator
