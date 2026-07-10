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

    ``store_path`` makes the learned high-water **durable across restarts**: a peak
    that a unit reached hours ago is not re-learned the hard way (i.e. via another OOM)
    after every service restart. The store is seeded on construction and rewritten
    (atomically, best-effort) whenever a unit's high-water rises. Stale values are safe
    by construction: the high-water only ever grows and over-reservation cannot OOM —
    a model that later shrinks merely over-reserves until the process is restarted with
    the store cleared.
    """

    def __init__(self, store_path: "Optional[str]" = None,
                 signature: "Optional[str]" = None) -> None:
        self._hw: Dict[str, float] = {}
        self._store_path = store_path
        # A store written under a different ``signature`` (e.g. a model/dtype/footprint
        # change) is DISCARDED on load rather than trusted: a stale value from a
        # different model could be too low, and under-reservation is the one dangerous
        # direction (OOM). ``None`` matches only ``None``.
        self._signature = signature
        if store_path:
            self._load()

    def record(self, unit: str, activation_bytes: float) -> None:
        """Directly raise ``unit``'s activation high-water (used by the scoped
        :class:`ActivationObserver`, which measures one op exactly). Monotonic —
        a smaller later measurement never lowers the reserve."""
        v = max(0.0, float(activation_bytes))
        if v > self._hw.get(unit, 0.0):
            self._hw[unit] = v
            self._save()

    def observe(self, peak_bytes: Optional[int], resident_weights_bytes: float,
                busy_units) -> None:
        """Legacy poll-sampler attribution (kept for tests / un-instrumented nodes):
        attribute the process high-water minus resident weights to the sole busy unit."""
        busy = list(busy_units)
        if peak_bytes is None or len(busy) != 1:
            return
        self.record(busy[0], float(peak_bytes) - float(resident_weights_bytes))

    def headroom_bytes(self, unit: str) -> float:
        return self._hw.get(unit, 0.0)

    # --- durable store (best-effort; never raises out) ----------------------
    def _load(self) -> None:
        try:
            import json
            with open(self._store_path) as f:
                data = json.load(f)
            # New format: {"signature": <sig>, "units": {name: bytes}}. Discard on
            # signature mismatch (and ignore the legacy flat format, which had none).
            if not isinstance(data, dict) or data.get("signature") != self._signature:
                return
            units = data.get("units") or {}
            self._hw = {str(k): float(v) for k, v in units.items()
                        if isinstance(v, (int, float)) and v >= 0}
        except FileNotFoundError:
            pass
        except Exception:
            pass  # corrupt/unreadable store — start fresh, don't crash the node

    def _save(self) -> None:
        if not self._store_path:
            return
        try:
            import json
            import os
            tmp = f"{self._store_path}.tmp.{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump({"signature": self._signature, "units": self._hw}, f)
            os.replace(tmp, self._store_path)  # atomic
        except Exception:
            pass


class ActivationObserver:
    """Brackets ONE GPU op to measure that unit's peak activation exactly — no sampling
    window, no cross-unit smear, no reliance on declared footprints.

    ``begin(unit)`` resets the allocator peak counter and records the *current* allocated
    bytes (the resident weights of all loaded units); ``end(unit)`` reads the since-reset
    high-water and records ``peak - baseline`` — precisely the transient this op added —
    as the unit's activation high-water. Driven by :meth:`ModelManager.run`, which is
    always called under the server's serialized GPU discipline (polyasr ``_transcribe_lock``
    / polytts single-thread executor), so a single in-flight baseline is race-free.

    This replaces the old 1 s poll sampler, whose window could attribute one unit's peak
    to whichever unit was ``last_ensured`` at sample time (under-reserving the real
    spiker) and whose baseline used declared footprints rather than measured weights."""

    def __init__(self, tracker: ActivationTracker, meter: "Optional[MemoryMeter]" = None):
        self._tracker = tracker
        self._meter = meter or _cuda_meter()
        self._base = 0.0

    def begin(self, unit: str) -> None:
        self._meter.reset_peak()
        self._base = float(self._meter.allocated())

    def end(self, unit: str) -> None:
        peak = float(self._meter.max_allocated())
        self._tracker.record(unit, peak - self._base)


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


def alloc_meter() -> "Optional[MemoryMeter]":
    """A CUDA allocator meter for :class:`ActivationObserver`, or ``None`` when torch/CUDA
    is absent — an MLX/CPU node simply doesn't live-measure and relies on the declared
    footprint plus any persisted headroom."""
    try:
        import torch
        if torch.cuda.is_available():
            return _cuda_meter()
    except Exception:
        pass
    return None
