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

import os
import threading
import time
from typing import Callable, Dict

from .coordinator import LivestackCoordinator
from .facade import build_router
from .lease import Capability


def _start_activation_sampler(manager, coordinator, peak_meter, tracker, interval):
    """Sample the process allocation high-water every ``interval`` s and attribute
    the excess over declared weights to the unit currently executing, learning its
    peak activation. Daemon thread; never raises out. Resets the peak each window so
    the next window's max is fresh (the allocator maintains the max continuously
    between resets, so a sub-second spike is still caught).

    The executing unit is ``manager.last_ensured`` (the unit a caller most recently
    ``ensure``d and is running on the serialized GPU thread) — NOT which units hold a
    residence lease. A batch commonly leases several units up front (e.g. media-corpus
    leases align+diarize together), so the old lease signal saw >1 "busy" unit and
    skipped every window, never learning diarize's peak. ``last_ensured`` is a single
    unit, so attribution is unambiguous and every unit that actually runs is measured.
    During idle the peak collapses to weights, so a stale ``last_ensured`` contributes
    ~0 excess and never inflates a headroom."""
    def loop():
        while True:
            time.sleep(interval)
            try:
                peak = peak_meter.peak_bytes()
                st = coordinator.status()
                resident = set(st.get("resident", []))
                exec_unit = manager.last_ensured
                busy = {exec_unit} if exec_unit in resident else set()
                weights = 0
                for k in resident:
                    u = manager.units.get(k)
                    weights += int(getattr(u, "footprint", 0) or 0) if u else 0
                tracker.observe(peak, weights, busy)
                peak_meter.reset()
            except Exception:
                pass
    t = threading.Thread(target=loop, name="harmony-activation-sampler", daemon=True)
    t.start()
    return t


def attach(app, *, host_id: str, kind: str, units: Dict[str, object],
           idle_seconds: int, coload: bool, gpu_call: Callable[[Callable], object],
           prefix: str = "/livestack", device_meter="auto"):
    """``device_meter``: a zero-arg callable -> measured {capacity,free} (see
    meters.py), ``"auto"`` to pick one by backend (CUDA/MLX), or ``None`` to report
    no live memory. Defaulting to "auto" means a node becomes memory-aware on
    redeploy with no server-side change.

    A per-process peak meter (also backend-auto) drives an :class:`ActivationTracker`
    on a background sampler, so the node also learns and reports each unit's peak
    *activation* headroom for the planner to reserve. Disable with
    ``LIVESTACK_ACT_SAMPLE_S=0``."""
    from polycore import ModelManager
    coordinator = LivestackCoordinator(host_id, coload=coload, usage_ttl_seconds=idle_seconds)
    manager = ModelManager(units, idle_seconds, coordinator=coordinator)
    if device_meter == "auto":
        from .meters import auto_meter
        device_meter = auto_meter()

    tracker = None
    sample_s = float(os.environ.get("LIVESTACK_ACT_SAMPLE_S", "1"))
    if sample_s > 0:
        from .meters import auto_peak_meter
        from .measure import ActivationTracker
        peak_meter = auto_peak_meter()
        if peak_meter is not None:
            # Learned activation high-waters persist across restarts so a unit's peak
            # is not re-discovered via OOM after every redeploy. Overridable; empty
            # string disables persistence.
            store = os.environ.get("LIVESTACK_ACT_STORE")
            if store is None:
                d = os.path.expanduser("~/.cache/livestack")
                try:
                    os.makedirs(d, exist_ok=True)
                    store = os.path.join(d, f"activation-{host_id}-{kind}.json")
                except Exception:
                    store = None
            tracker = ActivationTracker(store_path=store or None)
            _start_activation_sampler(manager, coordinator, peak_meter, tracker, sample_s)

    app.include_router(
        build_router(manager, coordinator, Capability(kind=kind, host_id=host_id),
                     gpu_call, device_meter=device_meter, activation_tracker=tracker),
        prefix=prefix,
    )
    return manager, coordinator
