"""ModelManager — Harmony's per-process *local executor* of model residency.

This is the seam shared by polyasr and polytts (which each used to embed a
mirrored copy as ``AsrModelManager`` / ``ModelManager``). The manager owns the
**side-effects** — model load/unload, the functional health-probe inference, the
idle clock — while the **decisions** (what to evict/load/recover, the resident-set
state machine, idle math, per-unit recover rate-limit) are delegated to a pure
Rust planner (``livestack_shared::residency``) exposed via the ``shared_py``
extension. One brain, shared by Python (here) and future TS services; no parallel
implementation to drift.

The default :class:`~livestack_node.coordinator.LocalCoordinator` reproduces today's
in-process discipline exactly (COLOAD + idle-evict), so a server that swaps its
embedded manager for this one sees no behavioural change. A ``LivestackCoordinator``
(shipped by livestack, never imported here) implements the same seam to arbitrate
VRAM across processes via the lease broker.

All load/unload calls must run on a single GPU executor thread, exactly like the
servers do today; the manager's light ``_guard`` only protects its own bookkeeping.
"""
from __future__ import annotations

import enum
import gc
import time
import threading
from contextlib import contextmanager
from typing import Callable, Optional

from .freeing import trim_ram

try:
    from shared_py import Planner as _Planner
except ImportError as _e:  # pragma: no cover - environment misconfiguration
    raise ImportError(
        "livestack_node requires the compiled `shared_py` residency planner (the Rust "
        "decision core). Build it into this venv:\n"
        "  cd ~/livestack/shared-py && VIRTUAL_ENV=\"$VIRTUAL_ENV\" "
        "PYO3_PYTHON=\"$(command -v python)\" python -m maturin develop --release"
    ) from _e


class ResidencyPolicy(enum.IntEnum):
    """Per-unit static policy. Wire ints match ``shared`` + the gRPC proto."""
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
                 min_resident: int = 0,
                 health_check: "Optional[Callable[[object], bool]]" = None):
        self.name = name
        self._loader = loader
        self._freer = freer
        self.footprint = footprint              # measured-and-cached bytes (0 = unknown)
        self.residency_policy = residency_policy
        self.min_resident = min_resident
        # Optional FUNCTIONAL liveness probe: given the loaded model, returns True
        # iff the unit is actually producing correct output. This is the signal a
        # heartbeat / process-alive / `/health` check cannot give — a unit can be
        # resident and answering health checks yet silently emit garbage (e.g. an
        # ASR partial path that returns empty after long uptime). None = no probe.
        self.health_check = health_check
        self.model: object = None
        self.loaded_at: Optional[float] = None

    @property
    def loaded(self) -> bool:
        return self.model is not None

    def check_health(self) -> "Optional[bool]":
        """Run the functional liveness probe. Returns None when the unit is not
        loaded or has no probe (health unknown / not applicable), otherwise the
        probe's verdict. A probe that raises counts as unhealthy (False) — a
        crashing probe is itself a degradation signal."""
        if self.model is None or self.health_check is None:
            return None
        try:
            return bool(self.health_check(self.model))
        except Exception:
            return False

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
    """Resident-unit side-effects (load/unload/probe) over a pure Rust planner.

    Policy/decisions are delegated to a Coordinator (which, locally, drives the
    Rust planner). GPU-thread only beyond the bookkeeping guard.

    The public surface (``ensure`` / ``touch`` / ``unload_now`` / ``maybe_evict`` /
    ``recover`` / ``maybe_recover_degraded`` / ``status`` / ``resident`` /
    ``last_used``) is a strict superset of the servers' current ``AsrModelManager``
    so callers migrate unchanged.
    """

    def __init__(self, units: dict[str, ManagedUnit], idle_seconds: int,
                 coload: bool = True, coordinator: "Coordinator | None" = None,
                 activation_observer: "Optional[object]" = None,
                 log: Callable[[str], None] = print):
        self.units = units
        self.idle_seconds = idle_seconds
        self.last_used = time.monotonic()
        # Optional per-op activation-measurement scope (livestack_node.ActivationObserver):
        # run() brackets the GPU op with observer.begin/end to learn the unit's peak
        # activation exactly. None => run() is a plain ensure()+call.
        self._act_observer = activation_observer
        # The most recently ``ensure``d unit — i.e. the one a caller is about to run
        # (servers call ensure(name) immediately before executing that unit's GPU op,
        # serialized on their GPU thread). This is a far more faithful "what is
        # actually executing" signal than which units hold a residence lease, so the
        # activation sampler attributes the peak-allocation high-water to it.
        self._last_ensured: Optional[str] = None
        self._guard = threading.Lock()
        self._log = log
        # The Rust decision core. State (resident set, recover rate-limit) lives in
        # the planner; the host only executes the side-effects it returns.
        self._planner = _Planner([
            (name, int(u.footprint), int(u.residency_policy), int(u.min_resident),
             u.health_check is not None)
            for name, u in units.items()
        ])
        # Local import to avoid a cycle; LocalCoordinator only references manager primitives.
        from .coordinator import LocalCoordinator
        self.coordinator = coordinator or LocalCoordinator(coload=coload)
        self.coordinator.bind(self)

    # --- primitives the coordinator drives (caller holds _guard, GPU thread) ------
    def _load(self, name: str) -> object:
        model = self.units[name].load()
        self._planner.commit_loaded(name)
        self._log(f"[harmony] loaded {name} (resident={self._planner.resident()})")
        return model

    def _evict(self, name: str) -> None:
        self.units[name].unload()
        self._planner.commit_evicted(name)
        self._log(f"[harmony] evicted {name} (resident={self._planner.resident()})")

    def _reload(self, name: str) -> object:
        """Evict (if resident) then load — the unit-level self-heal for a
        functionally degraded model. Cheaper than a process restart and the unit
        of recovery the broker reasons about (same mechanism as warm-floor
        reload). Caller holds ``_guard``; GPU thread."""
        evict, load = self._planner.plan_recover_one(name)
        for n in evict:
            self._evict(n)
        model = None
        for n in load:
            model = self._load(n)
        return model

    # --- public surface (parity with AsrModelManager) -----------------------------
    def ensure(self, name: str) -> object:
        """Make ``name`` resident, returning its model, per the coordinator's policy.
        Resets the idle timer. GPU-thread only."""
        if not self._planner.known(name):
            raise KeyError(f"unknown unit: {name}")
        with self._guard:
            model = self.coordinator.acquire(name)
            self.last_used = time.monotonic()
            self._last_ensured = name
            return model

    @contextmanager
    def run_scope(self, name: str):
        """``with manager.run_scope(name) as model: ...`` — make ``name`` resident and
        bracket the block with the activation observer: it resets the device peak counter
        on entry and, on exit, attributes (peak − measured resident baseline) to ``name``
        as its exact per-op activation, which the Harmony planner reserves so the unit
        never OOMs at its runtime peak. Callers MUST serialize GPU ops (polyasr
        ``_transcribe_lock`` / polytts single-thread executor already do); the observer
        keeps one in-flight baseline. With no observer wired this is a plain ``ensure``.
        GPU-thread only."""
        model = self.ensure(name)
        obs = self._act_observer
        if obs is None:
            yield model
            return
        obs.begin(name)
        try:
            yield model
        finally:
            obs.end(name)

    def run(self, name: str, thunk: "Callable[[object], object]"):
        """Thunk form of :meth:`run_scope`: ``manager.run(name, lambda model: ...)``."""
        with self.run_scope(name) as model:
            return thunk(model)

    def touch(self) -> None:
        """Reset the idle timer without (re)loading. Called on every active frame so
        a live session is never idle-evicted."""
        self.last_used = time.monotonic()

    def unload_now(self) -> list[str]:
        """Force-evict ALL resident units now, regardless of idle time. For hand-off."""
        with self._guard:
            evicted = self._planner.resident()      # sorted
            for name in evicted:
                self._evict(name)
            self.coordinator.on_release_all(evicted)
            if not evicted:
                trim_ram()
            return evicted

    def maybe_evict(self) -> bool:
        """Idle sweep, per coordinator policy. GPU-thread only."""
        with self._guard:
            return self.coordinator.idle_sweep()

    def recover(self, name: str) -> object:
        """Evict+reload ``name`` and notify the coordinator — the explicit
        unit-level self-heal for a functionally degraded model. Returns the
        reloaded model. GPU-thread only."""
        if not self._planner.known(name):
            raise KeyError(f"unknown unit: {name}")
        with self._guard:
            model = self._reload(name)
            self._planner.mark_recovered(name, time.monotonic())
            self.last_used = time.monotonic()
            self.coordinator.on_degraded(name)
            return model

    def maybe_recover_degraded(self, min_interval: float = 0.0) -> list[str]:
        """Functional-liveness sweep: for each resident unit that carries a
        ``health_check``, verify it and evict+reload any that report unhealthy.
        This catches the failure a heartbeat / ``/health`` probe cannot — a unit
        that is resident and process-alive yet silently produces wrong/empty
        output. Per-unit rate-limited by ``min_interval`` so a unit that reloads
        still-broken cannot hot-loop. Returns the names recovered. GPU-thread only.

        The driver (cadence + the unit-specific probe) lives in the server, exactly
        like ``maybe_evict`` is driven by the server's idle loop — polycore stays
        device-agnostic; it owns only the mechanism. The decision (which degraded
        units to reload, subject to the rate-limit) is the planner's."""
        with self._guard:
            candidates = self._planner.probe_candidates()
        # Probe each candidate's own inference OUTSIDE the guard so a slow probe
        # doesn't block status() reads. False == degraded; None/True == skip.
        degraded = [n for n in candidates if self.units[n].check_health() is False]
        if not degraded:
            return []
        recovered: list[str] = []
        with self._guard:
            now = time.monotonic()
            to_reload = self._planner.plan_recover(degraded, now, min_interval)
            rate_limited = set(degraded) - set(to_reload)
            for name in rate_limited:
                self._log(f"[harmony] {name} degraded but recover rate-limited")
            for name in to_reload:
                self._reload(name)
                self._planner.mark_recovered(name, now)
                self.last_used = now
                self.coordinator.on_degraded(name)
                recovered.append(name)
        return recovered

    def request_evict(self, name: str) -> None:
        """Broker/operator -> 'please unload ``name``', honoured per coordinator
        policy (e.g. pins are kept). GPU-thread only."""
        with self._guard:
            self.coordinator.on_evict_request(name)

    @property
    def resident(self) -> set[str]:
        return set(self._planner.resident())

    @property
    def last_ensured(self) -> Optional[str]:
        """The unit most recently made resident via :meth:`ensure` — the node's best
        signal for which unit is currently executing on the GPU (see ``__init__``)."""
        return self._last_ensured

    # Back-compat alias: coordinators (LocalCoordinator, livestack's
    # LivestackCoordinator) were written against the manager's old private
    # ``_resident`` set. The state now lives in the Rust planner; expose a
    # read-only, planner-backed view so those coordinators keep working
    # unchanged (membership / iteration / emptiness / sorted — never mutated
    # directly; writes go through _load/_evict).
    @property
    def _resident(self) -> set[str]:
        return set(self._planner.resident())

    def status(self) -> dict:
        resident = self._planner.resident()
        return {
            "resident": resident,
            "coload": getattr(self.coordinator, "coload", None),
            "idle_seconds": self.idle_seconds,
            "idle_for": (round(time.monotonic() - self.last_used, 1)
                         if resident else None),
            "units": list(self.units.keys()),
        }
