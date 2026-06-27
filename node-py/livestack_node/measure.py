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
