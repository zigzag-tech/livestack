"""measure.py — resource-footprint measurement for the planner.

A unit's planner ``footprint`` must be **weights + peak activation**, not resident
weights alone: the OOM that motivated all this was transient activation memory for
a long align chunk, not the model's weights. This measures the peak allocation
delta around loading (and optionally running) a unit and returns it as a planner
resource vector.

The CUDA meter is the default; a meter is injectable so the logic is unit-tested
without a GPU.
"""
from __future__ import annotations

from typing import Callable, Dict, Optional, Tuple


class MemoryMeter:
    """Duck-typed meter. ``allocated``/``max_allocated`` in bytes."""
    def reset_peak(self) -> None: ...        # pragma: no cover
    def allocated(self) -> int: ...          # pragma: no cover
    def max_allocated(self) -> int: ...      # pragma: no cover


def measure_footprint(load_fn: Callable[[], object],
                      run_fn: Optional[Callable[[object], object]] = None,
                      meter: Optional[MemoryMeter] = None,
                      dim: str = "vram_bytes") -> Tuple[object, Dict[str, float]]:
    """Load (and optionally exercise) a unit, returning ``(model, footprint)``.

    ``footprint[dim]`` = peak allocation observed minus the baseline = resident
    weights PLUS the transient activation high-water mark. Pass a representative
    ``run_fn`` (e.g. one real align chunk) to capture activation; without it you get
    weights only and should add a safety reserve on the device.
    """
    meter = meter or _cuda_meter()
    base = meter.allocated()
    meter.reset_peak()
    model = load_fn()
    if run_fn is not None:
        run_fn(model)
    peak = meter.max_allocated() - base
    weights = meter.allocated() - base
    return model, {dim: float(max(peak, weights, 0))}


class ActivationTracker:
    """Learns each unit's peak-activation headroom from the live allocator high-water.

    A node process's peak allocation (``PeakMeter.peak_bytes`` — see meters.py) minus
    the declared weights of its resident units = the transient activation of whatever
    ran in that sampling window. Attributed to the *sole* busy unit (a window with 0
    or >1 busy units is skipped, so the signal isn't smeared across units). Held as a
    per-unit high-water so a single large input raises the reserve for the rest of the
    process's life. Pure counter logic — unit-tested by driving :meth:`observe`
    without a GPU; the allocator sampling lives in the node sampler.

    The reported ``activation_headroom`` feeds ``planner.Unit.activation_headroom``,
    which the Harmony planner reserves on the device while the unit is resident.
    """

    def __init__(self) -> None:
        self._hw: Dict[str, float] = {}

    def observe(self, peak_bytes: Optional[int], resident_weights_bytes: float,
                busy_units) -> None:
        busy = list(busy_units)
        if peak_bytes is None or len(busy) != 1:
            return
        unit = busy[0]
        excess = max(0.0, float(peak_bytes) - float(resident_weights_bytes))
        if excess > self._hw.get(unit, 0.0):
            self._hw[unit] = excess

    def headroom_bytes(self, unit: str) -> float:
        return self._hw.get(unit, 0.0)


def _cuda_meter() -> MemoryMeter:  # pragma: no cover - requires torch+CUDA
    import torch

    class _CudaMeter:
        def reset_peak(self) -> None:
            torch.cuda.reset_peak_memory_stats()

        def allocated(self) -> int:
            return torch.cuda.memory_allocated()

        def max_allocated(self) -> int:
            return torch.cuda.max_memory_allocated()

    return _CudaMeter()
