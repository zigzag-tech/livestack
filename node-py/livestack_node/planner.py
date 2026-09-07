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
    # Peak-activation VRAM kept FREE on the device while this unit is resident, so
    # its runtime activation never OOMs. `footprint` is resident weights; a unit's
    # *real* peak is weights + activation, and reserving only weights is the OOM
    # (a long align chunk's transient activation) that motivated Harmony. Reserved
    # for as long as the unit is resident — not merely at load — so a later backfill
    # can't steal the space the unit needs when it next runs. A node MEASURES this
    # live (allocator high-water minus declared weights) and reports it, so the
    # reserve tracks reality. Default {} => reserve == footprint => zero behavior
    # change.
    activation_headroom: Res = field(default_factory=dict)
    # Contention class. Units in the same non-empty group are alternatives for
    # the same sort of work (two LLMs, two TTS engines) and are therefore the
    # units most likely to be demanded in alternation.
    #
    # It does NOT say "spread these". What separates them is DEMAND: see
    # `_contention_cost`. Placing a unit beside a sibling is free when the
    # sibling is never asked for and expensive when the queue is full of it,
    # because that is exactly when the two will evict each other turn after
    # turn. One-LLM-per-GPU is then something the planner arrives at from the
    # workload, not a rule it was told — and when demand stops alternating, it
    # stops paying to keep them apart.
    #
    # Why any of this is the planner's business rather than the operator's: the
    # alternative is pinning each service to a card (CUDA_VISIBLE_DEVICES),
    # which decides placement outside the planner and cannot adapt. Two 15 GB
    # LLMs pinned to one 24 GB card fail forever.
    spread_group: str = ""
    # What this unit IS, for requests that state a REQUIREMENT rather than a
    # name: {"class": "llm", "params_b": 27, "quant": "int4", "ctx": 262144}.
    # Free-form on purpose — the planner never interprets a key, it only
    # compares (see `_unit_satisfies`), so a new axis costs no planner change.
    attributes: Mapping[str, object] = field(default_factory=dict)


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
    # --- hosted backends -----------------------------------------------------
    # A hosted device is somebody else's GPU behind an API (Qwen ASR, a vendor
    # STT endpoint). It has NO residency: nothing loads, nothing evicts, nothing
    # idles out, and it can never be a preemption victim. What it does have is a
    # concurrency ceiling and a price, so it is scheduled by lease count rather
    # than by bytes.
    hosted: bool = False
    # Added to every placement option on this device. NEGATIVE prefers it over a
    # warm local replica (cost 0) — that is "hosted is the default, the GPU is
    # for everything else". POSITIVE makes it overflow-only: chosen when a local
    # placement would need a load or a preemption, not before. One knob, both
    # policies, and it is the only thing that has to change to flip them.
    cost_bias: float = 0.0
    # Health gate. A hosted endpoint that is rate-limiting or down is simply not
    # a candidate this cycle, so demand falls back to the GPU with no special
    # case anywhere else in the planner.
    available: bool = True


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
    # A REQUIREMENT instead of (or alongside) a kind: "any llm of at least 20B".
    #   requires={"class": "llm", "params_b>=": 20}
    # Keys are unit attributes, optionally suffixed with a comparison
    # (`>=`, `>`, `<=`, `<`, `!=`); bare means equality. When set and `kind` is
    # empty, the planner resolves the kind itself — which is what lets a queue
    # of mixed work reshuffle VRAM instead of a caller naming the model and
    # hoping it is the one that fits.
    requires: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class WorldState:
    devices: Tuple[Device, ...]
    units: Mapping[str, Unit]
    placements: Tuple[Placement, ...] = ()
    requests: Tuple[Request, ...] = ()
    now: float = 0.0
    # kind -> epoch when it was last evicted under pressure (for SOFT_PIN restore debounce)
    last_evicted_at: Mapping[str, float] = field(default_factory=dict)
    # kind -> demand for it: queued jobs plus a decayed count of recent ones.
    # This is what makes residency a consequence of the WORKLOAD rather than of
    # configuration. `requests` is only what is admissible right now; a batch of
    # a thousand queued title jobs and a thousand judge jobs is the fact that
    # decides whether two LLMs should share a card, and it is not visible in the
    # in-flight request list. The broker fills this from the queue it already
    # has. Absent (the default) it is zero for every kind and the planner
    # behaves exactly as it did before.
    demand: Mapping[str, float] = field(default_factory=dict)
    # device_id -> MEASURED free resource vector (e.g. {"vram_bytes": ...}) read live
    # off the device this cycle. When present, the planner reconciles it against the
    # static budget and uses the TIGHTER of the two — so placement tracks real free
    # memory (external processes, footprint drift, activation spikes the static
    # footprints miss) instead of pure footprint bookkeeping.
    measured_free: Mapping[str, Res] = field(default_factory=dict)


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
    # What the unit may actually USE on that device, after this cycle's
    # evictions. The planner is the only party that knows it: the node knows its
    # own units, the operator knows a fraction of a card, and neither can see
    # what the planner just freed. Handing it over is what stops a unit being
    # hand-sized in a config file to squeeze into whatever a particular card
    # happened to have left — which is placement decided by an operator again,
    # in a different disguise. Empty when the planner did not compute one.
    budget: Mapping[str, float] = field(default_factory=dict)
    # Why this grant landed here. Every other action already carried one; a
    # grant did not, so the ledger could record WHAT was admitted and never why
    # — which is the half a retrospective actually needs (see
    # `_plans/decision-ledger.md` §4.1). Defaulted, so no existing constructor
    # breaks.
    reason: str = ""


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
                parts.append(f"grant {a.request_id}->{a.kind}@{a.device_id}"
                             + (f" ({a.reason})" if a.reason else ""))
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
    # Scales the expected cost of future thrash between same-class units sharing
    # a device (see `_contention_cost`). The cost itself comes from the
    # WORKLOAD — a sibling's reload cost times how much demand is queued and
    # recently seen for it — so this is a weight on a measured quantity, not a
    # fixed penalty standing in for one.
    contention_weight: float = 1.0


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
        # Footprint sum resident PER DEVICE at snapshot time. A measured-free reading
        # corresponds to THIS resident set; as the planner loads/evicts this cycle,
        # real free shifts by the delta — so we adjust measured_free by it (below).
        self._used_at_snapshot: Dict[str, Dict[str, float]] = {d.id: {} for d in w.devices}
        for p in w.placements:
            if p.device_id in self._used_at_snapshot:
                self._used_at_snapshot[p.device_id] = _add(
                    self._used_at_snapshot[p.device_id], w.units[p.kind].footprint)

    def used(self, device_id: str) -> Dict[str, float]:
        u: Dict[str, float] = {}
        for p in self.resident[device_id].values():
            u = _add(u, self.w.units[p.kind].footprint)
        return u

    def _resident_headroom(self, device_id: str) -> Dict[str, float]:
        """Peak-activation VRAM kept FREE on the device for as long as each unit is
        resident, so a unit's runtime activation never OOMs (the align-chunk spike
        that motivated Harmony). Summed across resident units — conservative for
        units in separate processes that can peak concurrently; over-reserves (never
        under-reserves) when a single executor serializes them. Released on evict."""
        h: Dict[str, float] = {}
        for p in self.resident[device_id].values():
            hr = self.w.units[p.kind].activation_headroom
            if hr:
                h = _add(h, hr)
        return h

    def free(self, device_id: str) -> Dict[str, float]:
        d = self.devices[device_id]
        # Reserve resident units' measured peak-activation on top of the static
        # `reserved` slack, so admitting/backfilling another unit can't consume the
        # space a resident unit needs when it next runs (prevents runtime OOM, not
        # just load-time). Zero when no unit declares headroom => unchanged.
        hdrm = self._resident_headroom(device_id)
        budget = _sub(_sub(_sub(d.capacity, d.reserved), self.used(device_id)), hdrm)
        meas = self.w.measured_free.get(device_id) if self.w.measured_free else None
        if not meas:
            return budget
        # Reconcile model vs reality: keep the configured `reserved` headroom on top
        # of the *measured* free bytes, then take the tighter of (policy budget,
        # measured reality) per dimension. So neither a too-optimistic static model
        # (external process / drift) nor exceeding our self-imposed budget can grant
        # an allocation that would OOM. NOTE: this can go NEGATIVE when reality is
        # worse than the model assumed — step 0 of plan() sheds to relieve that.
        # Adjust the snapshot reading by what we've loaded/evicted so far this cycle:
        # delta = used_now - used_at_snapshot (positive => we loaded => less real free).
        delta = _sub(self.used(device_id), self._used_at_snapshot.get(device_id, {}))
        adjusted = _sub(meas, delta)
        avail = _sub(_sub(adjusted, d.reserved), hdrm)
        out = dict(budget)
        for k, v in avail.items():
            out[k] = min(budget.get(k, v), v)
        return out

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

    def grant(self, req: Request, device_id: str, reason: str = "",
              budget: Optional[Res] = None) -> None:
        self.actions.append(Grant(request_id=req.id, kind=req.kind,
                                  device_id=device_id, reason=reason,
                                  budget=dict(budget or {})))

    def defer(self, req: Request, reason: str) -> None:
        self.actions.append(Defer(request_id=req.id, reason=reason))


_CMPS = (">=", "<=", "!=", ">", "<")


def _unit_satisfies(u: Unit, requires: Mapping[str, object]) -> bool:
    """Does this unit meet every stated requirement?

    A key may carry a comparison suffix (`params_b>=`); bare keys are equality.
    A requirement naming an attribute the unit does not declare is NOT met —
    silence is not a yes, or an unlabelled unit would satisfy everything.
    """
    for key, want in requires.items():
        op = ""
        name = key
        for c in _CMPS:
            if key.endswith(c):
                op, name = c, key[: -len(c)]
                break
        name = name.strip()
        if name not in u.attributes:
            return False
        have = u.attributes[name]
        try:
            if op == "":
                if have != want:
                    return False
            elif op == "!=":
                if have == want:
                    return False
            elif op == ">=":
                if not float(have) >= float(want):
                    return False
            elif op == ">":
                if not float(have) > float(want):
                    return False
            elif op == "<=":
                if not float(have) <= float(want):
                    return False
            elif op == "<":
                if not float(have) < float(want):
                    return False
        except (TypeError, ValueError):
            return False                    # non-numeric where a number was needed
    return True


def candidate_kinds(world: WorldState, req: Request) -> List[str]:
    """Kinds this request could be served by, best first.

    A named `kind` is honoured as-is — naming a unit still means that unit. With
    only `requires`, every unit meeting it is a candidate, ordered so the
    planner tries the cheapest outcome first: already resident, then smaller
    footprint, then cheaper to load. That ordering is what makes a mixed queue
    settle instead of thrashing — a request for "an llm >= 20B" is served by the
    20B already on a card rather than loading a 27B beside it.
    """
    if req.kind:
        return [req.kind]
    if not req.requires:
        return []
    resident = {p.kind for p in world.placements}
    fits = [k for k, u in world.units.items() if _unit_satisfies(u, req.requires)]
    return sorted(fits, key=lambda k: (k not in resident,
                                       _magnitude(world.units[k].footprint),
                                       world.units[k].reload_cost, k))


def _hosted_has_room(world: "_World", d: Device) -> bool:
    """A hosted backend admits while its concurrent leases are under capacity.

    Local devices schedule RESIDENCE (one resident copy serves unlimited leases);
    a hosted backend has no residence to schedule, so its scarce resource is
    in-flight requests. An absent ``concurrency`` capacity means unmetered.
    """
    limit = d.capacity.get("concurrency")
    if limit is None:
        return True
    live = sum(p.leases for p in world.w.placements if p.device_id == d.id)
    return live < limit


def _device_matches(d: Device, selector: Mapping[str, str]) -> bool:
    return all(d.labels.get(k) == v for k, v in selector.items())


def _admission_need(unit: Unit) -> Res:
    """VRAM a device must have free to safely ADMIT/place a new load of ``unit``:
    resident weights (``footprint``) plus its transient peak-activation
    ``activation_headroom``. Only the admission/preemption fit checks use this;
    steady-state residence accounting (``_World.used``) still charges ``footprint``
    alone, so headroom prevents an OOM grant without permanently inflating the
    resident memory model. Default (no headroom) => ``footprint`` unchanged."""
    if not unit.activation_headroom:
        return unit.footprint
    return _add(unit.footprint, unit.activation_headroom)


def _eff_priority(req: Request, unit: Unit, now: float, pol: PlannerPolicy) -> int:
    base = req.priority if req.priority is not None else unit.priority
    waited = max(0.0, now - req.created_at)
    boost = min(pol.max_aging_boost, int(waited / pol.aging_interval_s) * pol.aging_step)
    return base - boost  # lower = more important


def _yields_at_equal_priority(world: _World, p: Placement, u: Unit,
                              requester_kind: str) -> bool:
    """May an EQUAL-priority resident be preempted?

    Only when it is UNPINNED, idle, and nobody is asking for it. Two units of
    the same class and priority — two LLMs on one card — could otherwise never
    displace each other, so a model nobody wants keeps the card from one that is
    being demanded right now, and the only way through was to evict by hand.
    That external evict is a race: anything can re-warm the unit in the seconds
    before the new one is placed, and the load then starts against a card that
    is no longer free (observed 2026-09-07, three seconds apart).

    Demand is the tie-break because it is the thing that distinguishes them:
    priority says how important a KIND is, demand says whether anyone wants it
    NOW. The comparison is RELATIVE — the card goes to whoever wants it more —
    not "the resident must be at exactly zero". Demand decays continuously, so a
    strict-zero test leaves a long tail where a model finished with hours ago
    still holds a card against one being actively requested (measured: 0.47
    against a live requester, 22 minutes after the last call).

    A busy or leased unit is never a victim here, so this cannot preempt work in
    flight — it only lets a card go to whoever is actually using it.
    """
    if u.residency != Residency.UNPINNED or p.busy or p.leases > 0:
        return False
    resident = float(world.w.demand.get(p.kind, 0.0))
    requester = float(world.w.demand.get(requester_kind, 0.0))
    return resident <= 0.0 or resident < requester


def _victims_to_free(world: _World, device_id: str, need: Res, requester_prio: int,
                     pol: PlannerPolicy, requester_kind: str = "") -> Optional[List[Placement]]:
    """Minimal set of evictable resident units on ``device_id`` whose removal makes
    ``need`` fit. Evictable = lower priority than the requester (or equal priority
    while UNPINNED, idle and unwanted — see below), not HARD_PIN, past its
    min-residency, and (by default) idle. Returns None if even evicting all
    evictables would not fit."""
    units = world.w.units
    cands: List[Placement] = []
    for p in world.resident[device_id].values():
        u = units[p.kind]
        if u.residency == Residency.HARD_PIN:
            continue
        if u.priority < requester_prio:         # more important: never a victim
            continue
        if u.priority == requester_prio and not _yields_at_equal_priority(
                world, p, u, requester_kind):
            continue                            # equal importance and still wanted
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


def _shed_victim(world: _World, device_id: str, pol: PlannerPolicy) -> Optional[Placement]:
    """The single least-important evictable resident unit on a device, used to
    relieve *measured* over-budget pressure when there is no pending request to
    drive eviction. Evictable = not HARD_PIN, past its min-residency (anti-thrash),
    and idle unless busy-preemption is allowed. None if nothing may be shed."""
    units = world.w.units
    cands: List[Placement] = []
    for p in world.resident[device_id].values():
        u = units[p.kind]
        if u.residency == Residency.HARD_PIN:
            continue
        if (world.w.now - p.loaded_at) < u.min_residency_s:
            continue
        if p.busy and not pol.allow_busy_preemption:
            continue
        cands.append(p)
    if not cands:
        return None
    # idle first, least-important (highest priority int), biggest help, cheapest reload
    cands.sort(key=lambda p: (p.busy, -units[p.kind].priority,
                              -_magnitude(units[p.kind].footprint),
                              units[p.kind].reload_cost))
    return cands[0]


@dataclass
class _Option:
    device_id: str
    cost: float
    victims: List[Placement]
    needs_load: bool
    # Resource left on this device AFTER the placement. Ties on cost are broken
    # by taking the TIGHTEST fit (best-fit bin packing): an 8 GB unit that can
    # sit beside an existing tenant should do so, and leave the empty card whole
    # for something that needs a whole card. First-fit leaves exactly the
    # fragmentation this avoids — two half-used cards and nowhere to put a 27B.
    slack: float = 0.0


def _contention_cost(world: _World, device_id: str, unit: Unit,
                     pol: PlannerPolicy) -> float:
    """Expected future thrash from putting ``unit`` on this device.

    For each same-class sibling already resident here that CANNOT co-reside with
    ``unit`` — the two together exceed what the device can hold — the pair will
    take turns evicting each other for as long as both are demanded. The
    expected price of that is the sibling's reload cost times how much demand is
    waiting for it, so:

        cost = w * SUM over contending siblings of  reload_cost(v) * demand(v)

    Three consequences, all of them the point:

    * A batch that alternates between two LLMs makes each one's demand high, so
      sharing a card costs a great deal and they settle one per card — without
      anything anywhere saying "one LLM per GPU".
    * A sibling nothing is asking for contributes nothing. Residency follows the
      workload, and stops paying to keep units apart when the alternation stops.
    * Siblings that genuinely FIT together are not contending at all and cost
      zero. Co-residence is only a problem when it cannot last.

    `demand` is queued + recently-seen work, so both halves of "historical
    precedence as well as what's upcoming" are in the same number.
    """
    if not unit.spread_group or pol.contention_weight <= 0:
        return 0.0
    need = _admission_need(unit)
    total = 0.0
    for p in world.resident.get(device_id, {}).values():
        if p.kind == unit.kind:
            continue                      # a warm copy of ourselves is reuse, not rivalry
        v = world.w.units.get(p.kind)
        if v is None or v.spread_group != unit.spread_group:
            continue
        # Do we actually contend? If both fit with the sibling still resident,
        # nobody evicts anybody and there is nothing to charge for.
        if _fits(need, world.free(device_id)):
            continue
        total += v.reload_cost * float(world.w.demand.get(p.kind, 0.0))
    return pol.contention_weight * total


def _best_placement(world: _World, req: Request, unit: Unit, pol: PlannerPolicy,
                    eff_prio: int) -> Optional[_Option]:
    """Cheapest feasible device for ``req``: warm-resident (cost 0) beats load-in-free
    beats load-after-preemption. Encodes the place-vs-preempt (腾挪-here vs
    migrate-there) decision under one cost function."""
    best: Optional[_Option] = None
    for d in world.w.devices:
        if not _device_matches(d, {**unit.selector, **req.selector}):
            continue
        if d.hosted:
            # No residency to arrange and no locality to speak of — the audio
            # leaves the host either way. Admission is purely "is it up, and is
            # there room in the concurrency budget", and the cost is the bias.
            if not d.available or not _hosted_has_room(world, d):
                continue
            opt = _Option(d.id, d.cost_bias, [], needs_load=False)
            if best is None or opt.cost < best.cost:
                best = opt
            continue
        loc_pen = 0.0 if (req.locality_host is None or req.locality_host == d.host_id) \
            else pol.locality_penalty
        # warm: a resident copy serves another lease for free
        if world.is_resident(req.kind, d.id):
            opt = _Option(d.id, 0.0 + loc_pen, [], needs_load=False,
                          slack=_magnitude(_sub(world.free(d.id), _admission_need(unit))))
        elif _fits(_admission_need(unit), world.free(d.id)):
            opt = _Option(d.id, unit.reload_cost + loc_pen
                          + _contention_cost(world, d.id, unit, pol), [], needs_load=True,
                          slack=_magnitude(_sub(world.free(d.id), _admission_need(unit))))
        else:
            victims = _victims_to_free(world, d.id, _admission_need(unit), eff_prio, pol,
                                       req.kind)
            if victims is None:
                continue
            preempt_cost = sum(world.w.units[v.kind].reload_cost for v in victims)
            busy_pen = sum(50.0 for v in victims if v.busy)   # discourage interrupting work
            freed = world.free(d.id)
            for v in victims:
                freed = _add(freed, world.w.units[v.kind].footprint)
            opt = _Option(d.id, unit.reload_cost + loc_pen + preempt_cost + busy_pen
                          + _contention_cost(world, d.id, unit, pol),
                          victims, needs_load=True,
                          slack=_magnitude(_sub(freed, _admission_need(unit))))
        if best is None or (opt.cost, opt.slack) < (best.cost, best.slack):
            best = opt
    return best


def _relocation_for(world: _World, victim: Placement, from_device: str,
                    pol: PlannerPolicy) -> Optional[str]:
    """Somewhere else this preempted unit fits right now, or None.

    Eviction under pressure treats a resident unit as expendable: it is dropped
    and only comes back through the SOFT_PIN restore, a debounce later. But when
    a big unit needs a whole card and a small one happens to be sitting on it,
    the right move is not to drop the small one — it is to MOVE it to the card
    that still has room. Same reload cost either way; the difference is whether
    it keeps serving.

    Only devices that fit it with no preemption of their own are candidates: a
    relocation that itself needs to evict somebody is a chain this planner does
    not attempt in one cycle.
    """
    unit = world.w.units.get(victim.kind)
    if unit is None:
        return None
    need = _admission_need(unit)
    best_id, best_slack = None, None
    for d in world.w.devices:
        if d.id == from_device or d.hosted:
            continue
        if not _device_matches(d, unit.selector):
            continue
        if not _fits(need, world.free(d.id)):
            continue
        slack = _magnitude(_sub(world.free(d.id), need))
        if best_slack is None or slack < best_slack:
            best_id, best_slack = d.id, slack
    return best_id


def plan(world: WorldState, policy: Optional[PlannerPolicy] = None) -> Plan:
    """Compute the residency/placement plan for ``world``. Pure function."""
    pol = policy or PlannerPolicy()
    W = _World(world)

    # 0) Relieve MEASURED over-budget pressure. If a device's reconciled free is
    #    negative — real free fell below what the static footprints assumed (an
    #    external process grabbed VRAM, a model is bigger than declared, etc.) —
    #    shed idle, non-pinned, least-important units until non-negative. Honours
    #    anti-thrash + idle-only; a no-op when free >= 0 (the steady state).
    for d in world.devices:
        if d.hosted:
            continue    # no bytes to reclaim, and its units are not evictable
        guard = 0
        while any(v < -_EPS for v in W.free(d.id).values()) and guard < 64:
            guard += 1
            victim = _shed_victim(W, d.id, pol)
            if victim is None:
                break
            W.evict(victim.kind, d.id, "relieve measured over-budget pressure")

    # 1) Honour pending demand, most-important (after aging) first, then FIFO.
    # `.get`, not `[]`. The loop below handles an unknown kind by deferring it
    # with a reason — but the SORT KEY ran first and raised KeyError, so the
    # whole plan died instead of one request being deferred. A broker turned
    # that exception into `granted: True` and a node loaded a model onto a card
    # the planner had never cleared. An unknown kind sorts last (there is
    # nothing to be urgent about) and is deferred where it always should have
    # been.
    _UNKNOWN = Unit(kind="", footprint={}, priority=1_000_000)

    def _unit_for(r: Request) -> Optional[Unit]:
        """First kind this request could be served by, for ordering purposes."""
        for k in candidate_kinds(world, r):
            u = world.units.get(k)
            if u is not None:
                return u
        return None

    reqs = sorted(
        world.requests,
        key=lambda r: (_eff_priority(r, _unit_for(r) or _UNKNOWN, world.now, pol),
                       r.created_at, r.id),
    )
    for req in reqs:
        # A request may NAME a unit or merely STATE WHAT IT NEEDS. The caller
        # should not have to know which model is loaded, on which card, or
        # whether anything has to move to make room — that is the whole point of
        # routing inference through a residency planner. Candidates come back
        # cheapest-outcome-first, so a requirement is served by something already
        # resident when one qualifies, and only otherwise causes a load.
        cands = [k for k in candidate_kinds(world, req) if k in world.units]
        if not cands:
            # Keep the reason specific: a named kind nobody registered and a
            # requirement nothing satisfies are different operator problems.
            W.defer(req, f"no unit satisfies {dict(req.requires)}" if req.requires
                    else "unknown kind")
            continue
        unit = None
        opt = None
        for kind in cands:
            u = world.units[kind]
            e = _eff_priority(req, u, world.now, pol)
            o = _best_placement(W, Request(**{**req.__dict__, "kind": kind}), u, pol, e)
            if o is not None:
                unit, opt, eff = u, o, e
                break
        if opt is None or unit is None:
            W.defer(req, "no device can fit even with preemption")
            continue
        req = Request(**{**req.__dict__, "kind": unit.kind})
        moved = {}
        for v in opt.victims:
            W.evict(v.kind, opt.device_id,
                    f"preempted by {req.kind} (prio {eff})")
            # Move it rather than drop it, when somewhere else has room. This is
            # the case the operator hits constantly: a small model took the empty
            # card, a model that needs a whole card arrives, and the small one
            # should step aside — not disappear until a debounce brings it back.
            dest = _relocation_for(W, v, opt.device_id, pol)
            if dest is not None:
                W.load(v.kind, dest, f"relocated from {opt.device_id} to make room for {req.kind}")
                moved[v.kind] = dest
        if opt.needs_load:
            W.load(req.kind, opt.device_id, f"demand: {req.id}")
        if opt.victims:
            dropped = sorted(v.kind for v in opt.victims if v.kind not in moved)
            parts = []
            if moved:
                parts.append("after relocating "
                             + ", ".join(f"{k}->{d}" for k, d in sorted(moved.items())))
            if dropped:
                parts.append("after evicting " + ", ".join(dropped))
            why = "; ".join(parts)
        elif opt.needs_load:
            why = "loaded on demand"
        else:
            why = "resident"
        # The budget is the device's free resources as of NOW in the working
        # copy — after the evictions above and the load below were committed —
        # which is exactly what this unit may occupy.
        W.grant(req, opt.device_id, why, budget=W.free(opt.device_id))

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
        if d.hosted:
            continue    # nothing to keep warm there; a pin floor it cannot hold
        if not _device_matches(d, unit.selector):
            continue
        if _fits(_admission_need(unit), world.free(d.id)):
            slack = _magnitude(world.free(d.id))
            if slack > best_free:
                best_free, best_dev = slack, d.id
        elif mandatory:
            victims = _victims_to_free(world, d.id, _admission_need(unit), unit.priority, pol)
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
