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
    # Devices where a node that actually SERVES this unit lives. Empty = unknown,
    # which means "anywhere" and is exactly the old behaviour.
    #
    # A device is not a card and not a server: several nodes share one card, and
    # they serve different things. Without this the planner ranked devices purely
    # on free space and picked one whose only tenant was a TTS server — a grant
    # nobody could honour. The request was then forwarded to that neighbour,
    # which answered 404 because it has no such endpoint, and the reply read like
    # the model was missing rather than like it had been sent to the wrong
    # server. Placement has to be to somewhere the unit can actually run.
    servable_on: frozenset = field(default_factory=frozenset)
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
    # A load DISPATCHED but not yet serving. It holds the card — the weights are
    # arriving — and it cannot answer a request yet, so it is neither "resident"
    # nor "absent" and both answers are wrong in a different place.
    #
    # Absent was the one the planner believed, and it cost a card: a 21.7 GB
    # model reports not-resident for the minutes it takes to load, so the
    # SOFT_PIN restore saw no copy, started a second one on the other card, and
    # a two-card host ended up holding one model twice with nowhere to put
    # anything else. `loaded_at` is when the load was DISPATCHED, which is what
    # makes anti-thrash protect an in-flight load like any other fresh one.
    loading: bool = False


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
    # True when `owner` was ASSERTED by a caller a fronting engine trusts (the
    # engine's inbound `X-Harmony-Owner` header), False when the engine admits
    # under its own identity because the caller named nobody. Travels with the
    # Request so the Grant record can mark the fact; defaulted so every
    # existing constructor is unchanged.
    owner_asserted: bool = False
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
    # WHOSE REQUEST brought this load about — the owner of the `Request` whose
    # planning produced it ("pressure" for a rule-0 shed, "hard-pin floor" for
    # a pin restore). The ledger names this next to the record, so a reload
    # is explained from the ledger alone: who needed the room, who brought it
    # back. Empty only for actions older than the field.
    caused_by: str = ""


@dataclass(frozen=True)
class Evict:
    kind: str
    device_id: str
    reason: str = ""
    # WHOSE REQUEST needed the room — same field, same contract as Load. The
    # 27B reload thrash of 2026-09-19 was diagnosed from journal timestamps
    # because the ledger could not say who needed the room; this is the fix.
    caused_by: str = ""


@dataclass(frozen=True)
class Grant:
    request_id: str
    kind: str
    device_id: str
    # Who is charged for this grant. Not derivable from `request_id` (an id
    # is not an identity), and the fact a retrospective needs: two apps, one
    # unit, two owners — the ledger must be able to say which owner each
    # Grant served. Filled from the Request; empty for actions built by hand.
    owner: str = ""
    # True when the owner arrived ASSERTED by a caller the engine trusts
    # (the `X-Harmony-Owner` header), False when the engine charged the
    # request to its own identity. The distinction is the difference between
    # "attune spent capacity" and "harmony-llm spent capacity on attune's
    # behalf without being told who was asking".
    owner_asserted: bool = False
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
        """Can this unit SERVE, here or anywhere? A loading copy cannot."""
        if device_id is not None:
            p = self.resident[device_id].get(kind)
            return p is not None and not p.loading
        return any(not p.loading for r in self.resident.values()
                   for k, p in r.items() if k == kind)

    def is_loading(self, kind: str, device_id: Optional[str] = None) -> bool:
        """Is a copy on its way — dispatched, holding the card, not yet serving?"""
        if device_id is not None:
            p = self.resident[device_id].get(kind)
            return p is not None and p.loading
        return any(p.loading for r in self.resident.values()
                   for k, p in r.items() if k == kind)

    def is_present(self, kind: str, device_id: Optional[str] = None) -> bool:
        """Does a copy EXIST OR IS ONE COMING? The question residency policy has
        to ask before placing another: a second copy of a unit already arriving
        wastes a whole card and serves nothing the first will not."""
        if device_id is not None:
            return kind in self.resident[device_id]
        return any(kind in r for r in self.resident.values())

    def replicas(self, kind: str) -> int:
        """Copies that exist or are arriving — see `is_present`. Counting only
        the ones that can serve makes a pin floor re-load an in-flight replica
        on every planning cycle until it finishes."""
        return sum(1 for r in self.resident.values() if kind in r)

    def load(self, kind: str, device_id: str, reason: str,
             caused_by: str = "") -> None:
        self.resident[device_id][kind] = Placement(kind=kind, device_id=device_id,
                                                    loaded_at=self.w.now)
        self.actions.append(Load(kind=kind, device_id=device_id, reason=reason,
                                 caused_by=caused_by))

    def evict(self, kind: str, device_id: str, reason: str,
              caused_by: str = "") -> None:
        self.resident[device_id].pop(kind, None)
        self.actions.append(Evict(kind=kind, device_id=device_id, reason=reason,
                                  caused_by=caused_by))

    def grant(self, req: Request, device_id: str, reason: str = "",
              budget: Optional[Res] = None) -> None:
        self.actions.append(Grant(request_id=req.id, kind=req.kind,
                                  device_id=device_id, reason=reason,
                                  budget=dict(budget or {}),
                                  owner=req.owner,
                                  owner_asserted=req.owner_asserted))

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


# SPECIALIST-ONLY ATTRIBUTES. A unit declaring one (e.g. `ocr: true`) serves
# work that generic demand can never imply — nothing derives `ocr` from a
# request, and no generic consumer states it — so the ONLY way such a unit ever
# gets selected is a requirement that happened to match on a shared attribute
# like `class: llm`. That selection is pure harm: it stops a real LLM unit to
# load a specialist model that answers language work badly. Measured on
# xc-tower-ubuntu 2026-09-19: a generic {class: llm} request was placed on
# `ocr_ovis2`, which stopped the resident 27B to do it. Naming the unit still
# works — an explicit choice never passes through candidate selection.
_SPECIALIST_ONLY_ATTRS = ("ocr",)


def _specialist_only(u: Unit, requires: Mapping[str, object]) -> bool:
    """Unit declares a specialist attribute the requirement does not name."""
    for attr in _SPECIALIST_ONLY_ATTRS:
        if attr in requires:
            continue
        val = (u.attributes or {}).get(attr)
        if val is True or (isinstance(val, str)
                           and val.strip().lower() in ("true", "1", "yes")):
            return True
    return False


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
    fits = [k for k, u in world.units.items()
            if _unit_satisfies(u, req.requires)
            and not _specialist_only(u, req.requires)]
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


def _can_serve(unit: Unit, d: Device) -> bool:
    """Does a node that serves this unit live on this device? Unknown => yes,
    so a peer that reports no device placement constrains nothing."""
    return not unit.servable_on or d.id in unit.servable_on


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


def _yields_at_equal_priority(_world: _World, p: Placement, u: Unit,
                              _requester_kind: str) -> bool:
    """May an EQUAL-priority resident be preempted?

    Only when it is UNPINNED and idle. Two units of
    the same class and priority — two LLMs on one card — could otherwise never
    displace each other, so a model nobody wants keeps the card from one that is
    being demanded right now, and the only way through was to evict by hand.
    That external evict is a race: anything can re-warm the unit in the seconds
    before the new one is placed, and the load then starts against a card that
    is no longer free (observed 2026-09-07, three seconds apart).

    Reaching this function already means the planner is handling a live request.
    Historical demand must not veto that request: a high-volume model otherwise
    starves a lower-volume peer even after becoming idle. This happened when a
    title model's accumulated demand prevented the embedding model from ever
    taking their shared card, and then in reverse after embedding was loaded.
    The unit's minimum-residency window is the anti-thrash guard between swaps.

    A busy or leased unit is never a victim here, so this cannot preempt work in
    flight — it only lets a card go to whoever is actually using it.
    """
    if u.residency != Residency.UNPINNED or p.busy or p.leases > 0:
        return False
    return True


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


def _residency_floor_blocker(world: _World, device_id: str, need: Res,
                             requester_prio: int, pol: PlannerPolicy,
                             requester_kind: str = "") -> Optional[tuple]:
    """`(kind, min_residency_s, age_s)` of a load whose residency floor is what
    stands between ``need`` and this device — or None when the floor is not the
    blocker.

    Tells a Defer apart from an ordinary "no room": "no device can fit even
    with preemption" and "a 27B loaded 20 s ago is protected for 60 s" want
    opposite responses (wait vs widen), and the ledger record has to say which
    one a retrospective is looking at. The floor blocks when evicting every
    resident past its floor would still leave `need` unplaced, but evicting
    the young ones TOO would fit: the only thing between the request and the
    device is the anti-thrash protection, and protection is a wait, not a
    refusal of the request's worth.
    """
    units = world.w.units
    evictable: List[Placement] = []
    young: List[Placement] = []
    for p in world.resident[device_id].values():
        u = units[p.kind]
        if u.residency == Residency.HARD_PIN:
            continue
        if u.priority < requester_prio:         # more important: never a victim
            continue
        if u.priority == requester_prio and not _yields_at_equal_priority(
                world, p, u, requester_kind):
            continue
        if p.busy and not pol.allow_busy_preemption:
            continue
        (young if (world.w.now - p.loaded_at) < u.min_residency_s
         else evictable).append(p)
    freed = dict(world.free(device_id))
    if _fits(need, freed):
        return None                             # it fits as-is; no blocker
    for p in evictable:
        freed = _add(freed, units[p.kind].footprint)
    if _fits(need, freed):
        return None                             # the room exists without the young
    for p in young:
        freed = _add(freed, units[p.kind].footprint)
    if not _fits(need, freed):
        return None                             # even everything would not fit
    # Only the floor stands between: name the largest protected load.
    p = max(young, key=lambda p: _magnitude(units[p.kind].footprint))
    return (p.kind, units[p.kind].min_residency_s,
            world.w.now - p.loaded_at)


def _shed_victim(world: _World, device_id: str, pol: PlannerPolicy,
                 wanted: "Optional[set]" = None) -> Optional[Placement]:
    """The single least-important evictable resident unit on a device, used to
    relieve *measured* over-budget pressure when there is no pending request to
    drive eviction. Evictable = not HARD_PIN, past its min-residency (anti-thrash),
    and idle unless busy-preemption is allowed. None if nothing may be shed.

    `wanted` is the set of kinds pending demand could be served by, and nothing
    in it is shed here. Evicting the unit the queue is waiting for relieves
    nothing — the space frees, the next rule loads it straight back, and on a
    27B that round trip is 2m15s of loading during which every caller gets a
    503. Demand-driven eviction still happens; it happens in rule 1, where the
    request that needs the room decides what moves."""
    units = world.w.units
    cands: List[Placement] = []
    for p in world.resident[device_id].values():
        u = units[p.kind]
        if u.residency == Residency.HARD_PIN:
            continue
        if wanted and p.kind in wanted:
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
        if not _can_serve(unit, d):
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
        elif world.is_loading(req.kind, d.id):
            # A copy is already arriving here. Waiting for it needs no second
            # load and no second card, so it must cost LESS than loading again
            # elsewhere — otherwise the loading copy's own footprint makes its
            # card look full, the free card looks cheaper, and the request
            # duplicates the very unit it is waiting for. It costs MORE than
            # zero, so a copy that can serve now still wins (see the warm branch
            # above): half a reload is the expected wait, having arrived at a
            # uniformly random point during it.
            opt = _Option(d.id, unit.reload_cost / 2.0 + loc_pen, [], needs_load=False,
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
    # WHAT THE QUEUE IS WAITING FOR IS NOT SPARE CAPACITY.
    #
    # Shedding the unit pending demand wants relieves nothing: the space frees,
    # rule 1 loads it straight back, and nobody is served in between. On a 27B
    # that round trip is 2m15s. Measured on xc-tower-ubuntu 2026-09-18, with
    # three applications all asking for the same abliterated 27B: loaded
    # 21:54:21, evicted 21:54:23; loaded 21:56:50, evicted 21:56:51; loaded
    # 22:18:46, evicted 22:18:50 — "relieve measured over-budget pressure"
    # every time, on a card whose only tenant WAS the thing being asked for.
    # The model spent its life loading and every caller got a 503.
    #
    # Demand-driven eviction is unaffected: rule 1 evicts what it must to place
    # a request, and it will not evict a unit to make room for itself.
    wanted_kinds: set = set()
    for _r in world.requests:
        wanted_kinds.update(candidate_kinds(world, _r))

    for d in world.devices:
        if d.hosted:
            continue    # no bytes to reclaim, and its units are not evictable
        guard = 0
        while any(v < -_EPS for v in W.free(d.id).values()) and guard < 64:
            guard += 1
            victim = _shed_victim(W, d.id, pol, wanted_kinds)
            if victim is None:
                break
            # AND, with nothing pending at all, do not empty the device.
            #
            # `wanted_kinds` is empty between requests, and a warm-on-start
            # load lands exactly there: unit resident, queue empty, reconciled
            # free negative because that unit is large. Shedding the only
            # tenant then relieves nothing — no one is waiting for the space,
            # and the soft-pin reloads it. Measured on xc-tower-ubuntu after
            # the `wanted_kinds` guard was already deployed: loaded 22:43:00,
            # evicted 22:43:05, for the fourth time that hour.
            #
            # With demand present this does not apply, and rule 1 still evicts
            # whatever a request needs it to.
            if not world.requests and len(W.resident[d.id]) <= 1:
                break
            W.evict(victim.kind, d.id, "relieve measured over-budget pressure",
                    caused_by="pressure")

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
            reason = "no device can fit even with preemption"
            # Say WHY when the why is a residency floor: a young load that
            # would otherwise be the victim is protected, and the caller
            # should wait for the floor, not give up on the request.
            blocker = next(
                (b for d in world.devices if not d.hosted
                 for b in [_residency_floor_blocker(
                     W, d.id, _admission_need(u), e, pol, kind)] if b),
                None)
            if blocker is not None:
                b_kind, b_floor, b_age = blocker
                reason = (f"residency floor: {b_kind} loaded {b_age:.0f}s ago "
                          f"is protected for {b_floor:.0f}s (min_residency_s); "
                          f"the room exists behind the floor")
            W.defer(req, reason)
            continue
        req = Request(**{**req.__dict__, "kind": unit.kind})
        moved = {}
        for v in opt.victims:
            W.evict(v.kind, opt.device_id,
                    f"preempted by {req.kind} (prio {eff})",
                    caused_by=req.owner)
            # Move it rather than drop it, when somewhere else has room. This is
            # the case the operator hits constantly: a small model took the empty
            # card, a model that needs a whole card arrives, and the small one
            # should step aside — not disappear until a debounce brings it back.
            dest = _relocation_for(W, v, opt.device_id, pol)
            if dest is not None:
                W.load(v.kind, dest, f"relocated from {opt.device_id} to make room for {req.kind}",
                       caused_by=req.owner)
                moved[v.kind] = dest
        if opt.needs_load:
            W.load(req.kind, opt.device_id, f"demand: {req.id}",
                   caused_by=req.owner)
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
        # `is_present`, not `is_resident`: a copy already on its way is a copy.
        # Asking "can it serve yet?" here is what started a second one beside it.
        if unit.residency != Residency.SOFT_PIN or W.is_present(kind):
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
        if not _can_serve(unit, d):
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
        world.load(kind, best_dev, "soft-pin restore" if not mandatory else "hard-pin floor",
                   caused_by="soft-pin restore" if not mandatory else "hard-pin floor")
        return True
    if mandatory and victims_for:
        dev = min(victims_for, key=lambda k: sum(world.w.units[v.kind].reload_cost
                                                 for v in victims_for[k]))
        for v in victims_for[dev]:
            world.evict(v.kind, dev, f"preempted for hard-pin {kind}",
                        caused_by="hard-pin floor")
        world.load(kind, dev, "hard-pin floor", caused_by="hard-pin floor")
        return True
    return False
