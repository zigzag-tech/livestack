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

import math
from typing import Callable, Dict, Iterable, Optional, Tuple

# A load that moved this process's allocator by less than this left its weights
# somewhere the meter cannot see (an engine in another process, the CPU). It is
# not evidence of a footprint, and recording it would plan a model at ~0 bytes.
MIN_RESIDENT_EVIDENCE = 64 * 1024 * 1024
# Upper bound on any learned value. Larger than any device; a store holding more
# was not written by this code and is treated as corrupt.
MAX_LEARNED_BYTES = float(1 << 40)


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

    It also learns each unit's RESIDENT bytes (:meth:`record_resident`, measured by
    :meth:`ActivationObserver.measure_load`), so a unit's planner footprint is what
    loading it took, not an operator's number. Resident + activation are the unit's
    whole measured cost; /residence reports them with ``footprint_source``
    ``"allocator"`` once both are known (facade.py). Same rules as the high-water:
    the declared footprint is the prior until the first measurement, a learned value
    is only raised by evidence, at most one entry per known unit, and a store that
    cannot be read is reported (``store_error``), never mistaken for an empty one.
    """

    def __init__(self, store_path: "Optional[str]" = None,
                 signature: "Optional[str]" = None,
                 known_units: "Optional[Iterable[str]]" = None) -> None:
        self._hw: Dict[str, float] = {}
        # Learned RESIDENT bytes per unit: what loading it took from this process's
        # allocator (see ActivationObserver.measure_load). Absent = never measured,
        # and the declared footprint stands as the prior.
        self._resident: Dict[str, float] = {}
        self._store_path = store_path
        # A store written under a different ``signature`` (e.g. a model/dtype/footprint
        # change) is DISCARDED on load rather than trusted: a stale value from a
        # different model could be too low, and under-reservation is the one dangerous
        # direction (OOM). ``None`` matches only ``None``.
        self._signature = signature
        # Bound: the store holds at most one entry per unit this process serves.
        # Entries for any other name are dropped on load and refused on record.
        self._known = frozenset(known_units) if known_units is not None else None
        # Why the durable store could not be used, or None. A store that exists but
        # cannot be read is a FAILURE, distinct from a store that does not exist yet:
        # the learned values it held are lost, and /residence says so.
        self.store_error: Optional[str] = None
        if store_path:
            self._load()

    def _accepts(self, unit: str) -> bool:
        return self._known is None or unit in self._known

    def record(self, unit: str, activation_bytes: float) -> None:
        """Directly raise ``unit``'s activation high-water (used by the scoped
        :class:`ActivationObserver`, which measures one op exactly). Monotonic —
        a smaller later measurement never lowers the reserve."""
        if not self._accepts(unit):
            return
        v = max(0.0, float(activation_bytes))
        if unit not in self._hw or v > self._hw[unit]:
            self._hw[unit] = v
            self._save()

    def record_resident(self, unit: str, resident_bytes: float) -> bool:
        """Learn what ``unit`` holds on the device once loaded.

        The first measurement REPLACES the declared prior (it is evidence; the prior
        was not). After that the value only rises: a smaller later load is the
        allocator reusing cached blocks, not the model shrinking. A load that moved
        this process's allocator by less than ``MIN_RESIDENT_EVIDENCE`` is no
        evidence at all — the weights live somewhere this meter cannot see (a
        separate engine process, the CPU) — and is ignored rather than recorded
        as a zero footprint. Returns whether the value was taken."""
        v = float(resident_bytes)
        if not self._accepts(unit) or not math.isfinite(v) or v < MIN_RESIDENT_EVIDENCE:
            return False
        if v > self._resident.get(unit, 0.0):
            self._resident[unit] = v
            self._save()
        return True

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

    def has_activation(self, unit: str) -> bool:
        """Has an op of ``unit`` been measured (even one that added nothing)?"""
        return unit in self._hw

    def resident_bytes(self, unit: str) -> Optional[float]:
        """Learned resident bytes, or None when never measured (declared prior stands)."""
        return self._resident.get(unit)

    # --- durable store (never raises out; failures are recorded in store_error) --
    def _load(self) -> None:
        import json
        try:
            with open(self._store_path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return                                   # absence: nothing learned yet
        except Exception as e:                       # failure: unreadable / not JSON
            self._quarantine(f"unreadable: {type(e).__name__}: {e}")
            return
        # Discard on signature mismatch (and the legacy flat format, which had none):
        # a deliberate reset, not a failure.
        if not isinstance(data, dict) or data.get("signature") != self._signature:
            return
        hw, res = {}, {}
        for key, out in (("units", hw), ("resident", res)):
            block = data.get(key) or {}
            if not isinstance(block, dict):
                self._quarantine(f"invalid: {key!r} is not an object")
                return
            for k, v in block.items():
                if (isinstance(v, bool) or not isinstance(v, (int, float))
                        or not math.isfinite(v) or v < 0 or v > MAX_LEARNED_BYTES):
                    self._quarantine(f"invalid: {key}[{k!r}] = {v!r}")
                    return
                if self._accepts(str(k)):
                    out[str(k)] = float(v)
        self._hw, self._resident = hw, res

    def _quarantine(self, why: str) -> None:
        """Keep the bad file for inspection (one slot, overwritten: bounded) and
        start from the declared priors, saying so."""
        self.store_error = f"{self._store_path}: {why}"
        try:
            import os
            os.replace(self._store_path, f"{self._store_path}.corrupt")
        except Exception:
            pass

    def _save(self) -> None:
        if not self._store_path:
            return
        try:
            import json
            import os
            tmp = f"{self._store_path}.tmp.{os.getpid()}"
            with open(tmp, "w") as f:
                json.dump({"signature": self._signature, "units": self._hw,
                           "resident": self._resident}, f)
            os.replace(tmp, self._store_path)  # atomic
        except Exception as e:
            # Learned values still apply in this process; they will not survive a
            # restart, and the report says so instead of pretending they will.
            self.store_error = f"{self._store_path}: save failed: {type(e).__name__}: {e}"


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
        self._base_reserved: "Optional[float]" = None
        # Why the last load could not be measured, or None.
        self.load_error: "Optional[str]" = None

    def begin(self, unit: str) -> None:
        self._meter.reset_peak()
        self._base = float(self._meter.allocated())
        self._base_reserved = _reserved(self._meter)

    def end(self, unit: str) -> None:
        peak = float(self._meter.max_allocated()) - self._base
        # What the op took from the CARD is the allocator's reserved growth, which
        # fragmentation makes larger than the allocated growth (klein, 2026-10-02:
        # 2.8 GiB allocated, 4.2 GiB more reserved-but-unallocated at its OOM). Take
        # the larger of the two: either is a floor on what the op needed.
        if self._base_reserved is not None:
            peak = max(peak, float(self._meter.max_reserved()) - self._base_reserved)
        self._tracker.record(unit, peak)

    def measure_load(self, unit: str, load: "Callable[[], object]") -> object:
        """Run ``load`` and learn what it left resident in this process's allocator.

        Measured as the growth in reserved (else allocated) bytes across the load,
        so co-resident units' weights and the CUDA context sit in the baseline and
        are not attributed to ``unit``. A meter that fails is recorded in the
        observer's ``load_error`` and never fails the load itself."""
        try:
            before = _reserved(self._meter)
            before_alloc = float(self._meter.allocated())
        except Exception as e:
            self.load_error = f"{unit}: meter failed before load: {type(e).__name__}: {e}"
            return load()
        model = load()
        try:
            grew = float(self._meter.allocated()) - before_alloc
            if before is not None:
                grew = max(grew, _reserved(self._meter) - before)
            self._tracker.record_resident(unit, grew)
        except Exception as e:
            self.load_error = f"{unit}: meter failed after load: {type(e).__name__}: {e}"
        return model


def _reserved(meter) -> "Optional[float]":
    """The meter's reserved bytes, or None for a meter that only counts allocations."""
    fn = getattr(meter, "reserved", None)
    return float(fn()) if fn is not None else None


def _cuda_meter() -> MemoryMeter:  # pragma: no cover - requires torch+CUDA
    import torch

    class _CudaMeter:
        def reset_peak(self) -> None:
            torch.cuda.reset_peak_memory_stats()

        def allocated(self) -> int:
            return torch.cuda.memory_allocated()

        def max_allocated(self) -> int:
            return torch.cuda.max_memory_allocated()

        def reserved(self) -> int:
            return torch.cuda.memory_reserved()

        def max_reserved(self) -> int:
            return torch.cuda.max_memory_reserved()

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
