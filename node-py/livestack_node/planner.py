"""planner.py — generalized resource-aware, priority-preemptive placement planner.

The brain livestack uses to decide **what is resident where** when demand exceeds
capacity. It is a *pure function* of a :class:`WorldState` -> :class:`Plan` (an
ordered list of actions): no I/O, no device calls, an injectable ``now``. The same
``plan()`` governs one GPU, one host, or the whole mesh — federation only changes
how the WorldState is *assembled* and how the resulting actions are *dispatched*
(see ``_plans/resource-planner.md``). That is what makes this a generalized
livestack capability rather than a GPU-specific hack.

Generalized over RESOURCES: a footprint/capacity is a vector of named scalar
dimensions — ``{"vram_bytes": ...}`` today, plus ``ram_bytes`` / ``cpu`` /
``npu`` / ``license_slots`` / ``throughput`` tomorrow — so the same planner
schedules GPU residence now and any finite resource later.

Residency tiers (mirror ``polycore.ResidencyPolicy`` but kept dependency-free):

* ``HARD_PIN``  — fleet keeps >= ``min_resident`` warm; never preempted, never the
  last replica evicted. (e.g. ASR.)
* ``SOFT_PIN``  — preferred-warm but **preemptible** under pressure; restored when
  the pressure settles (with hysteresis). (e.g. TTS.)
* ``UNPINNED``  — pure demand residence; first evicted, last restored. (e.g. chipgen.)

Two anti-pathology guards are built in:

* **anti-thrash**: a freshly-loaded unit is protected by ``min_residency_s`` before
  it may be preempted; a preempted SOFT_PIN waits ``restore_debounce_s`` after the
  pressure releases before it is restored.
* **anti-starvation**: a deferred request's *effective* priority ages upward the
  longer it waits, so low-priority work cannot be starved forever.

Default preemption is **idle-only**: a unit that holds an active (heartbeating)
lease is never preempted — you wait for it or place elsewhere. Trading time for
space (时间换空间) is expressed as :class:`Defer`. Set ``allow_busy_preemption`` to
let a strictly-higher-priority request interrupt busy lower-priority work.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Optional, Tuple, Union

_EPS = 1e-9

# --- generic resource vectors ------------------------------------------------
Res = Mapping[str, float]


def _sub(a: Res, b: Res) -> Dict[str, float]:
    return {k: a.get(k, 0.0) - b.get(k, 0.0) for k in set(a) | set(b)}


def _add(a: Res, b: Res) -> Dict[str, float]:
    return {k: a.get(k, 0.0) + b.get(k, 0.0) for k in set(a) | set(b)}


def _fits(need: Res, avail: Res) -> bool:
    """Does ``need`` fit within ``avail`` on every dimension it touches?"""
    return all(need.get(k, 0.0) <= avail.get(k, 0.0) + _EPS for k in need)


def _magnitude(r: Res) -> float:
    """A scalar size used only for victim tie-breaks (sum across dims)."""
    return sum(max(0.0, v) for v in r.values())


# --- model ------------------------------------------------------------------
class Residency(enum.IntEnum):
    HARD_PIN = 0
    SOFT_PIN = 1
    UNPINNED = 2


@dataclass(frozen=True)
class Unit:
    """A loadable, shareable resident thing (a model unit). One resident copy
    serves unlimited concurrent leases, so residence — not per-lease packing — is
    what the planner schedules."""
    kind: str
    footprint: Res                                  # weights + PEAK activation headroom
    priority: int = 100                             # lower = more important
    residency: Residency = Residency.UNPINNED
    min_resident: int = 0                           # fleet-wide warm floor (HARD_PIN)
    reload_cost: float = 1.0                        # ~seconds to load; tie-break weight
    selector: Mapping[str, str] = field(default_factory=dict)   # device labels required
    min_residency_s: float = 15.0                  # anti-thrash: no preempt this soon after load
    restore_debounce_s: float = 20.0               # anti-thrash: wait after pressure before restore


@dataclass(frozen=True)
class Device:
    """A placement target with finite capacity. Generic: a GPU, a CPU pool, an
    NPU, a license server. ``reserved`` is permanent slack (e.g. activation
    headroom that pinning-by-weights alone would ignore — the real cause of the
    OOM that motivated this)."""
    id: str
    host_id: str
    capacity: Res
    reserved: Res = field(default_factory=dict)
    labels: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Placement:
    """A unit currently resident on a device."""
    kind: str
    device_id: str
    loaded_at: float = 0.0
    busy: bool = False              # holds >= 1 active (heartbeating) lease right now
    leases: int = 0


@dataclass(frozen=True)
class Request:
    """A pending demand for a unit to be resident & granted (a lease request)."""
    id: str
    kind: str
    owner: str = "anon"
    created_at: float = 0.0
    priority: Optional[int] = None              # default: the unit's priority
    selector: Mapping[str, str] = field(default_factory=dict)
    locality_host: Optional[str] = None         # where the input lives (placement pref)


@dataclass(frozen=True)
class WorldState:
    devices: Tuple[Device, ...]
    units: Mapping[str, Unit]
    placements: Tuple[Placement, ...] = ()
    requests: Tuple[Request, ...] = ()
    now: float = 0.0
    # kind -> epoch when it was last evicted under pressure (for SOFT_PIN restore debounce)
    last_evicted_at: Mapping[str, float] = field(default_factory=dict)


# --- actions ----------------------------------------------------------------
@dataclass(frozen=True)
class Load:
    kind: str
    device_id: str
    reason: str = ""


@dataclass(frozen=True)
class Evict:
    kind: str
    device_id: str
    reason: str = ""


@dataclass(frozen=True)
class Grant:
    request_id: str
    kind: str
    device_id: str


@dataclass(frozen=True)
class Defer:
    request_id: str
    reason: str = ""


Action = Union[Load, Evict, Grant, Defer]


@dataclass(frozen=True)
class Plan:
    actions: Tuple[Action, ...]

    def of(self, cls) -> List[Action]:
        return [a for a in self.actions if isinstance(a, cls)]

    def summary(self) -> str:
        parts = []
        for a in self.actions:
            if isinstance(a, Load):
                parts.append(f"load {a.kind}@{a.device_id}")
            elif isinstance(a, Evict):
                parts.append(f"evict {a.kind}@{a.device_id}")
            elif isinstance(a, Grant):
                parts.append(f"grant {a.request_id}->{a.kind}@{a.device_id}")
            elif isinstance(a, Defer):
                parts.append(f"defer {a.request_id} ({a.reason})")
        return "; ".join(parts)


@dataclass(frozen=True)
class PlannerPolicy:
    aging_interval_s: float = 30.0      # every interval waited, effective priority improves...
    aging_step: int = 5                 # ...by this many points (lower = more important)
    max_aging_boost: int = 80           # cap so aging can't invert HARD/UNPINNED tiers entirely
    allow_busy_preemption: bool = False # interrupt busy lower-priority work for a higher req?
    locality_penalty: float = 2.0       # cost added when placing off the data's host


# --- the planner ------------------------------------------------------------
class _World:
    """Mutable working copy the greedy planner mutates as it commits decisions."""

    def __init__(self, w: WorldState):
        self.w = w
        self.devices = {d.id: d for d in w.devices}
        # device_id -> {kind: Placement}
        self.resident: Dict[str, Dict[str, Placement]] = {d.id: {} for d in w.devices}
        for p in w.placements:
            if p.device_id in self.resident:
                self.resident[p.device_id][p.kind] = p
        self.actions: List[Action] = []

    def used(self, device_id: str) -> Dict[str, float]:
        u: Dict[str, float] = {}
        for p in self.resident[device_id].values():
            u = _add(u, self.w.units[p.kind].footprint)
        return u

    def free(self, device_id: str) -> Dict[str, float]:
        d = self.devices[device_id]
        return _sub(_sub(d.capacity, d.reserved), self.used(device_id))

    def is_resident(self, kind: str, device_id: Optional[str] = None) -> bool:
        if device_id is not None:
            return kind in self.resident[device_id]
        return any(kind in r for r in self.resident.values())

    def replicas(self, kind: str) -> int:
        return sum(1 for r in self.resident.values() if kind in r)

    def load(self, kind: str, device_id: str, reason: str) -> None:
        self.resident[device_id][kind] = Placement(kind=kind, device_id=device_id,
                                                    loaded_at=self.w.now)
        self.actions.append(Load(kind=kind, device_id=device_id, reason=reason))

    def evict(self, kind: str, device_id: str, reason: str) -> None:
        self.resident[device_id].pop(kind, None)
        self.actions.append(Evict(kind=kind, device_id=device_id, reason=reason))

    def grant(self, req: Request, device_id: str) -> None:
        self.actions.append(Grant(request_id=req.id, kind=req.kind, device_id=device_id))

    def defer(self, req: Request, reason: str) -> None:
        self.actions.append(Defer(request_id=req.id, reason=reason))


def _device_matches(d: Device, selector: Mapping[str, str]) -> bool:
    return all(d.labels.get(k) == v for k, v in selector.items())


def _eff_priority(req: Request, unit: Unit, now: float, pol: PlannerPolicy) -> int:
    base = req.priority if req.priority is not None else unit.priority
    waited = max(0.0, now - req.created_at)
    boost = min(pol.max_aging_boost, int(waited / pol.aging_interval_s) * pol.aging_step)
    return base - boost  # lower = more important


def _victims_to_free(world: _World, device_id: str, need: Res, requester_prio: int,
                     pol: PlannerPolicy) -> Optional[List[Placement]]:
    """Minimal set of evictable resident units on ``device_id`` whose removal makes
    ``need`` fit. Evictable = strictly-lower priority than the requester, not
    HARD_PIN, past its min-residency, and (by default) idle. Returns None if even
    evicting all evictables would not fit."""
    units = world.w.units
    cands: List[Placement] = []
    for p in world.resident[device_id].values():
        u = units[p.kind]
        if u.residency == Residency.HARD_PIN:
            continue
        if u.priority <= requester_prio:        # equal/higher importance: never a victim
            continue
        if (world.w.now - p.loaded_at) < u.min_residency_s:   # anti-thrash
            continue
        if p.busy and not pol.allow_busy_preemption:
            continue
        cands.append(p)
    # Prefer: idle first, least-important (highest priority int) first, biggest help,
    # cheapest to reload later.
    cands.sort(key=lambda p: (p.busy, -units[p.kind].priority,
                              -_magnitude(units[p.kind].footprint),
                              units[p.kind].reload_cost))
    chosen: List[Placement] = []
    freed = dict(world.free(device_id))
    if _fits(need, freed):
        return []
    for p in cands:
        chosen.append(p)
        freed = _add(freed, units[p.kind].footprint)
        if _fits(need, freed):
            return chosen
    return None


@dataclass
class _Option:
    device_id: str
    cost: float
    victims: List[Placement]
    needs_load: bool


def _best_placement(world: _World, req: Request, unit: Unit, pol: PlannerPolicy,
                    eff_prio: int) -> Optional[_Option]:
    """Cheapest feasible device for ``req``: warm-resident (cost 0) beats load-in-free
    beats load-after-preemption. Encodes the place-vs-preempt (腾挪-here vs
    migrate-there) decision under one cost function."""
    best: Optional[_Option] = None
    for d in world.w.devices:
        if not _device_matches(d, {**unit.selector, **req.selector}):
            continue
        loc_pen = 0.0 if (req.locality_host is None or req.locality_host == d.host_id) \
            else pol.locality_penalty
        # warm: a resident copy serves another lease for free
        if world.is_resident(req.kind, d.id):
            opt = _Option(d.id, 0.0 + loc_pen, [], needs_load=False)
        elif _fits(unit.footprint, world.free(d.id)):
            opt = _Option(d.id, unit.reload_cost + loc_pen, [], needs_load=True)
        else:
            victims = _victims_to_free(world, d.id, unit.footprint, eff_prio, pol)
            if victims is None:
                continue
            preempt_cost = sum(world.w.units[v.kind].reload_cost for v in victims)
            busy_pen = sum(50.0 for v in victims if v.busy)   # discourage interrupting work
            opt = _Option(d.id, unit.reload_cost + loc_pen + preempt_cost + busy_pen,
                          victims, needs_load=True)
        if best is None or opt.cost < best.cost:
            best = opt
    return best


def plan(world: WorldState, policy: Optional[PlannerPolicy] = None) -> Plan:
    """Compute the residency/placement plan for ``world``. Pure function."""
    pol = policy or PlannerPolicy()
    W = _World(world)

    # 1) Honour pending demand, most-important (after aging) first, then FIFO.
    reqs = sorted(
        world.requests,
        key=lambda r: (_eff_priority(r, world.units[r.kind], world.now, pol), r.created_at, r.id),
    )
    for req in reqs:
        unit = world.units.get(req.kind)
        if unit is None:
            W.defer(req, "unknown kind")
            continue
        eff = _eff_priority(req, unit, world.now, pol)
        opt = _best_placement(W, req, unit, pol, eff)
        if opt is None:
            W.defer(req, "no device can fit even with preemption")
            continue
        for v in opt.victims:
            W.evict(v.kind, opt.device_id,
                    f"preempted by {req.kind} (prio {eff})")
        if opt.needs_load:
            W.load(req.kind, opt.device_id, f"demand: {req.id}")
        W.grant(req, opt.device_id)

    # 2) HARD_PIN floor: guarantee >= min_resident warm replicas (mandatory).
    for kind, unit in world.units.items():
        if unit.residency != Residency.HARD_PIN:
            continue
        floor = max(1, unit.min_resident) if unit.min_resident else 1
        while W.replicas(kind) < floor:
            placed = _place_warm(W, kind, unit, pol, mandatory=True)
            if not placed:
                break

    # 3) SOFT_PIN restore (best-effort, debounced): bring preferred-warm units back
    #    once pressure has settled and there is room WITHOUT preempting anyone.
    for kind, unit in world.units.items():
        if unit.residency != Residency.SOFT_PIN or W.is_resident(kind):
            continue
        evicted_at = world.last_evicted_at.get(kind)
        if evicted_at is not None and (world.now - evicted_at) < unit.restore_debounce_s:
            continue   # still cooling down — don't thrash
        _place_warm(W, kind, unit, pol, mandatory=False)

    return Plan(tuple(W.actions))


def _place_warm(world: _World, kind: str, unit: Unit, pol: PlannerPolicy,
                mandatory: bool) -> bool:
    """Make ``kind`` resident on the best device with free room. For ``mandatory``
    (HARD_PIN floor) preemption of lower-priority idle units is allowed; for
    best-effort (SOFT_PIN restore) only free space is used."""
    best_dev = None
    best_free = -1.0
    victims_for: Dict[str, List[Placement]] = {}
    for d in world.w.devices:
        if not _device_matches(d, unit.selector):
            continue
        if _fits(unit.footprint, world.free(d.id)):
            slack = _magnitude(world.free(d.id))
            if slack > best_free:
                best_free, best_dev = slack, d.id
        elif mandatory:
            victims = _victims_to_free(world, d.id, unit.footprint, unit.priority, pol)
            if victims is not None:
                victims_for[d.id] = victims
    if best_dev is not None:
        world.load(kind, best_dev, "soft-pin restore" if not mandatory else "hard-pin floor")
        return True
    if mandatory and victims_for:
        dev = min(victims_for, key=lambda k: sum(world.w.units[v.kind].reload_cost
                                                 for v in victims_for[k]))
        for v in victims_for[dev]:
            world.evict(v.kind, dev, f"preempted for hard-pin {kind}")
        world.load(kind, dev, "hard-pin floor")
        return True
    return False
