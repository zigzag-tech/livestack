"""polycore.ModelManager — the per-process *local executor* of model residency.

This is the zero-dependency seam shared by polyasr and polytts (which today each
embed a mirrored copy as ``AsrModelManager`` / ``ModelManager``). The manager owns
the resident set and the load/unload/measure primitives; the *policy* of what to
load or evict is delegated to a :class:`~polycore.coordinator.Coordinator`.

The default :class:`~polycore.coordinator.LocalCoordinator` reproduces today's
in-process discipline exactly (COLOAD + idle-evict), so a server that swaps its
embedded manager for this one sees no behavioural change. A
``LivestackCoordinator`` (shipped by livestack, never imported here) implements the
same seam to arbitrate VRAM across processes via the lease broker.

All load/unload calls must run on a single GPU executor thread, exactly like the
servers do today; the manager's light ``_guard`` only protects its own bookkeeping.
"""
from __future__ import annotations

import enum
import gc
import time
import threading
from typing import Callable, Optional

from .freeing import trim_ram


class ResidencyPolicy(enum.IntEnum):
    """Per-unit static policy. Mirrors the gRPC wire ints in the design doc."""
    HARD_PIN = 0    # fleet guarantees >= min_resident warm; never evict the last one
    SOFT_PIN = 1    # preferred-warm, evictable under pressure
    UNPINNED = 2    # pure demand-driven (default)


class ManagedUnit:
    """A loadable model unit.

    loader() -> the loaded model object(s) (opaque to the manager).
    freer()  -> backend-specific GPU/Metal cache free (e.g. free_cuda/free_mlx).

    ``footprint``/``residency_policy``/``min_resident`` are declarative metadata the
    coordinator reports to the broker; they do not change local behaviour.
    """

    def __init__(self, name: str, loader: Callable[[], object],
                 freer: Callable[[], None],
                 footprint: int = 0,
                 residency_policy: ResidencyPolicy = ResidencyPolicy.UNPINNED,
                 min_resident: int = 0):
        self.name = name
        self._loader = loader
        self._freer = freer
        self.footprint = footprint              # measured-and-cached bytes (0 = unknown)
        self.residency_policy = residency_policy
        self.min_resident = min_resident
        self.model: object = None
        self.loaded_at: Optional[float] = None

    @property
    def loaded(self) -> bool:
        return self.model is not None

    def load(self) -> object:
        if self.model is None:
            self.model = self._loader()
            self.loaded_at = time.monotonic()
        return self.model

    def unload(self) -> None:
        self.model = None
        self.loaded_at = None
        gc.collect()
        self._freer()
        trim_ram()


class ModelManager:
    """Resident-unit bookkeeping + load/unload primitives. Policy is delegated to a
    Coordinator. GPU-thread only beyond the bookkeeping guard.

    The public surface (``ensure`` / ``touch`` / ``unload_now`` / ``maybe_evict`` /
    ``status`` / ``resident`` / ``last_used``) is a strict superset of the servers'
    current ``AsrModelManager`` so callers migrate unchanged.
    """

    def __init__(self, units: dict[str, ManagedUnit], idle_seconds: int,
                 coload: bool = True, coordinator: "Coordinator | None" = None,
                 log: Callable[[str], None] = print):
        self.units = units
        self.idle_seconds = idle_seconds
        self._resident: set[str] = set()
        self.last_used = time.monotonic()
        self._guard = threading.Lock()
        self._log = log
        # Local import to avoid a cycle; LocalCoordinator only references manager primitives.
        from .coordinator import LocalCoordinator
        self.coordinator = coordinator or LocalCoordinator(coload=coload)
        self.coordinator.bind(self)

    # --- primitives the coordinator drives (caller holds _guard, GPU thread) ------
    def _load(self, name: str) -> object:
        model = self.units[name].load()
        self._resident.add(name)
        self._log(f"[polycore] loaded {name} (resident={sorted(self._resident)})")
        return model

    def _evict(self, name: str) -> None:
        self.units[name].unload()
        self._resident.discard(name)
        self._log(f"[polycore] evicted {name} (resident={sorted(self._resident)})")

    # --- public surface (parity with AsrModelManager) -----------------------------
    def ensure(self, name: str) -> object:
        """Make ``name`` resident, returning its model, per the coordinator's policy.
        Resets the idle timer. GPU-thread only."""
        if name not in self.units:
            raise KeyError(f"unknown unit: {name}")
        with self._guard:
            model = self.coordinator.acquire(name)
            self.last_used = time.monotonic()
            return model

    def touch(self) -> None:
        """Reset the idle timer without (re)loading. Called on every active frame so
        a live session is never idle-evicted."""
        self.last_used = time.monotonic()

    def unload_now(self) -> list[str]:
        """Force-evict ALL resident units now, regardless of idle time. For hand-off."""
        with self._guard:
            evicted = sorted(self._resident)
            for name in list(self._resident):
                self._evict(name)
            self.coordinator.on_release_all(evicted)
            if not evicted:
                trim_ram()
            return evicted

    def maybe_evict(self) -> bool:
        """Idle sweep, per coordinator policy. GPU-thread only."""
        with self._guard:
            return self.coordinator.idle_sweep()

    def request_evict(self, name: str) -> None:
        """Broker/operator -> 'please unload ``name``', honoured per coordinator
        policy (e.g. pins are kept). GPU-thread only."""
        with self._guard:
            self.coordinator.on_evict_request(name)

    @property
    def resident(self) -> set[str]:
        return self._resident

    def status(self) -> dict:
        return {
            "resident": sorted(self._resident),
            "coload": getattr(self.coordinator, "coload", None),
            "idle_seconds": self.idle_seconds,
            "idle_for": (round(time.monotonic() - self.last_used, 1)
                         if self._resident else None),
            "units": list(self.units.keys()),
        }
