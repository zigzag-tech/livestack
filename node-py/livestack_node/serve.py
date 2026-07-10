"""attach() — the one call both polyasr and polytts make, identically, to become
livestack nodes. The server builds its ManagedUnits (with per-unit
ResidencyPolicy for pinning) and supplies a ``gpu_call`` that runs a thunk under
its GPU discipline; everything else is uniform.

    manager, coord = attach(app, host_id="zz-tower0", kind="polyasr",
                            units=units, idle_seconds=180, coload=True,
                            gpu_call=gpu_call)

Only ``coload`` (True for polyasr's co-resident units, False for polytts'
one-model-in-VRAM engines) and which unit is HARD_PIN differ between the two.
"""
from __future__ import annotations

import hashlib
import os
from typing import Callable, Dict

from .coordinator import LivestackCoordinator
from .facade import build_router
from .lease import Capability


def _footprint_signature(units: Dict[str, object]) -> str:
    """A short fingerprint of the unit set + declared footprints. The persisted
    activation store is keyed by this, so a model/footprint change invalidates stale
    values instead of trusting a possibly-too-low reserve (the OOM direction)."""
    items = sorted((str(n), int(getattr(u, "footprint", 0) or 0)) for n, u in units.items())
    return hashlib.sha256(repr(items).encode()).hexdigest()[:16]


def attach(app, *, host_id: str, kind: str, units: Dict[str, object],
           idle_seconds: int, coload: bool, gpu_call: Callable[[Callable], object],
           prefix: str = "/livestack", device_meter="auto"):
    """``device_meter``: a zero-arg callable -> measured {capacity,free} (see
    meters.py), ``"auto"`` to pick one by backend (CUDA/MLX), or ``None`` to report
    no live memory. Defaulting to "auto" means a node becomes memory-aware on
    redeploy with no server-side change.

    Each ``manager.run()`` GPU op is bracketed by an :class:`ActivationObserver` that
    measures that unit's exact peak activation and reports it as headroom for the planner
    to reserve. Learned high-waters persist across restarts (keyed by a footprint
    signature, so a model change discards stale values). Disable with
    ``LIVESTACK_ACT_SAMPLE_S=0``; absent CUDA (MLX/CPU) the node just doesn't
    live-measure."""
    from .manager import ModelManager
    from .measure import ActivationTracker, ActivationObserver, alloc_meter

    if device_meter == "auto":
        from .meters import auto_meter
        device_meter = auto_meter()

    tracker = None
    observer = None
    if os.environ.get("LIVESTACK_ACT_SAMPLE_S", "1") != "0":
        meter = alloc_meter()
        if meter is not None:
            store = os.environ.get("LIVESTACK_ACT_STORE")
            if store is None:
                d = os.path.expanduser("~/.cache/livestack")
                try:
                    os.makedirs(d, exist_ok=True)
                    store = os.path.join(d, f"activation-{host_id}-{kind}.json")
                except Exception:
                    store = None
            tracker = ActivationTracker(store_path=store or None,
                                        signature=_footprint_signature(units))
            observer = ActivationObserver(tracker, meter=meter)

    coordinator = LivestackCoordinator(host_id, coload=coload, usage_ttl_seconds=idle_seconds)
    manager = ModelManager(units, idle_seconds, coordinator=coordinator,
                           activation_observer=observer)

    app.include_router(
        build_router(manager, coordinator, Capability(kind=kind, host_id=host_id),
                     gpu_call, device_meter=device_meter, activation_tracker=tracker),
        prefix=prefix,
    )
    return manager, coordinator
