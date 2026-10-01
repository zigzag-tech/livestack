"""composition.py — what each LLM device SHOULD run, judged, never applied.

`planner.py` decides what is resident where; this module decides what a vLLM
unit is made of: its base model, its LoRA adapters, KV dtype, context length
and batch cap. On 2026-09-28 that was decided by hand: adding a second adapter
(`jemm`) with bf16 KV would have left 22k KV tokens for a 24,576-token context
and vLLM would have refused to start; fp8 KV made it fit. Every number needed
to know that in advance was already in the engine's own startup lines
(`vllm_startup.py`). This module turns those measurements into a feasibility
rule and a cost, and picks the cheapest feasible composition — or `keep`.

Pure, like the planner: no I/O, no clock, no inference. `now` is state. The
only file this module ever opens is the cost-weights artifact, via
`load_weights(path)`.

Three separations carry the design (openspec change
`harmony-placement-foundation`, design §5, §7, §8):

* **Feasibility is code.** `feasible()` never reads a cost weight; no tunable
  number may turn an engine that cannot start into one that "can".
* **No measurement, no feasibility.** A prediction is an exact measured row, or
  an additive composition of measured deltas marked `estimated`; a term with no
  measured basis is `Unknown`, never `Feasible` and never 0.
* **Deciders propose, the same functions judge.** A `Composer` returns
  candidates; `run_composition` scores them and the live composition with the
  same `feasible` and `cost`, and keeps the live one unless a candidate beats
  it by more than the change costs. It returns a record; it writes nothing.
"""
from __future__ import annotations

import hashlib
import itertools
import json
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import (Dict, FrozenSet, List, Mapping, Optional, Protocol, Sequence, Tuple,
                    Union)

from . import composition_replay as rp
from .vllm_startup import GIB, CompositionKey, MeasuredCost, composition_hash

# A measured row's budget is printed rounded to 0.01 GiB; the device's budget is
# capacity × fraction to the byte. Within this they are the same budget.
BUDGET_TOL = int(0.011 * GIB)
LEDGER_CANDIDATE_CAP = 64       # the ledger's own cap on candidate rows
MAX_CANDIDATES = 200_000        # ExhaustiveComposer refuses a larger space
SELF_TRAFFIC_PREFIX = "jingway"
DEFAULT_MARGIN = 0.10


# --- the decision variable -----------------------------------------------------

@dataclass(frozen=True)
class Adapter:
    name: str
    base: str
    rank: int


@dataclass(frozen=True)
class Composition:
    """One device's vLLM launch, reduced to the facts that decide its memory and
    its queueing. `max_loras` is derived, as harmony-llm already does."""
    device: str
    base: str
    adapters: FrozenSet[str] = frozenset()
    kv_dtype: str = "auto"
    max_model_len: int = 0
    max_num_seqs: int = 0

    @property
    def max_loras(self) -> int:
        return len(self.adapters)

    def slot_rank(self, catalogue: Mapping[str, Adapter]) -> int:
        """harmony-llm launches with `--max-lora-rank` = the largest adapter
        rank, so every slot is sized for it."""
        return max((catalogue[a].rank for a in self.adapters if a in catalogue), default=0)

    def key(self, catalogue: Mapping[str, Adapter], engine_version: str) -> CompositionKey:
        """The key harmony-llm hashes at launch. An adapter missing from the
        catalogue hashes at rank 0 so an infeasible proposal still has an id to
        be recorded under; `feasible` rejects it by name."""
        return CompositionKey(
            base=self.base,
            adapters=tuple(sorted((a, catalogue[a].rank if a in catalogue else 0)
                                  for a in self.adapters)),
            kv_dtype=self.kv_dtype or "auto", max_model_len=self.max_model_len,
            max_num_seqs=self.max_num_seqs, engine_version=engine_version)

    def to_json(self) -> dict:
        return {"device": self.device, "base": self.base, "adapters": sorted(self.adapters),
                "kv_dtype": self.kv_dtype, "max_model_len": self.max_model_len,
                "max_num_seqs": self.max_num_seqs, "max_loras": self.max_loras}


def launch_flags(c: Composition) -> FrozenSet[str]:
    """The launch flags whose novelty is a risk. `auto` KV and zero LoRA slots
    are vLLM's defaults and pass no flag. The fp8 KV of 2026-09-28 is the
    example: the host had never started with it, and it failed until a gcc-14
    drop-in was added."""
    flags = {f"--max-model-len={c.max_model_len}", f"--max-num-seqs={c.max_num_seqs}"}
    if (c.kv_dtype or "auto") != "auto":
        flags.add(f"--kv-cache-dtype={c.kv_dtype}")
    if c.max_loras:
        flags.add(f"--max-loras={c.max_loras}")
    return frozenset(flags)


# --- state -----------------------------------------------------------------------

@dataclass(frozen=True)
class DeviceSpec:
    id: str
    capacity: int                   # bytes, the whole card
    budget_fraction: float          # --gpu-memory-utilization

    @property
    def budget(self) -> int:
        return int(self.capacity * self.budget_fraction)


@dataclass(frozen=True)
class EngineFacts:
    """What this device's engine can do, recorded as data rather than folklore:
    fp8 KV on xc-tower-ubuntu needs the NVCC_PREPEND_FLAGS gcc-14 drop-in, so
    it is listed only where that drop-in exists."""
    engine_version: str
    kv_dtypes: FrozenSet[str] = frozenset({"auto"})
    flags_started: FrozenSet[str] = frozenset()     # every launch flag ever started with


@dataclass(frozen=True)
class SearchSpace:
    kv_dtypes: Tuple[str, ...] = ("auto",)
    max_model_lens: Tuple[int, ...] = ()
    max_num_seqs: Tuple[int, ...] = ()


@dataclass(frozen=True)
class CompositionState:
    """Everything a composition decision reads. Demand records are dicts with
    the demand-log fields: ts, unit, composition_hash, adapter|None, owner_ns,
    prompt_tokens|None, completion_tokens|None, elapsed_ms, queue_ms|None,
    outcome."""
    devices: Tuple[DeviceSpec, ...]
    adapters: Mapping[str, Adapter] = field(default_factory=dict)
    bases: Tuple[str, ...] = ()
    measured: Mapping[str, MeasuredCost] = field(default_factory=dict)       # hash -> cost
    measured_keys: Mapping[str, CompositionKey] = field(default_factory=dict)  # hash -> key
    trace: Tuple[Mapping, ...] = ()
    live: Mapping[str, Composition] = field(default_factory=dict)            # device -> live
    engine: Mapping[str, EngineFacts] = field(default_factory=dict)          # device -> facts
    hard_pins: FrozenSet[Tuple[str, str]] = frozenset()                      # (device, base)
    search: SearchSpace = field(default_factory=SearchSpace)
    unit_bases: Mapping[str, str] = field(default_factory=dict)              # unit -> base
    windows: Tuple[Tuple[float, float], ...] = ()                            # scoring windows
    now: float = 0.0

    def device(self, device_id: str) -> DeviceSpec:
        for d in self.devices:
            if d.id == device_id:
                return d
        raise KeyError(device_id)

    def record_base(self, r: Mapping) -> Optional[str]:
        a = r.get("adapter")
        if a:
            return self.adapters[a].base if a in self.adapters else None
        return self.unit_bases.get(r.get("unit"))


def measured_rows(pairs: Sequence[Tuple[CompositionKey, MeasuredCost]]
                  ) -> Tuple[Dict[str, MeasuredCost], Dict[str, CompositionKey]]:
    """(key, cost) pairs -> the two `CompositionState` maps, keyed by hash."""
    costs, keys = {}, {}
    for k, m in pairs:
        h = composition_hash(k)
        costs[h], keys[h] = m, k
    return costs, keys


def is_self_traffic(r: Mapping) -> bool:
    """Jingway's own requests (§8 of scheduler-policy-routine): served, recorded,
    never allowed to argue for a composition."""
    return str(r.get("owner_ns") or "").startswith(SELF_TRAFFIC_PREFIX)


# --- memory prediction -------------------------------------------------------------

@dataclass(frozen=True)
class MemoryPrediction:
    weights: int
    activation: int
    cuda_graphs: int
    kv_bytes: int
    kv_tokens: int
    budget: int
    estimated: bool
    basis: Tuple[str, ...]          # measured hashes the prediction rests on
    margin: float = 0.0
    assumptions: Tuple[str, ...] = ()
    graphs_delta: int = 0           # the part of `cuda_graphs` that came from deltas

    @property
    def kv_tokens_checked(self) -> int:
        """KV is the residual, so every error in the other terms lands in it;
        an estimated row's margin is taken there. An exact row has none."""
        return int(self.kv_tokens * (1.0 - self.margin)) if self.estimated else self.kv_tokens

    @property
    def cuda_graphs_checked(self) -> int:
        """Graphs are checked directly against the space outside the budget, so
        the margin is taken on the part that was derived, not on a value an
        anchor row measured: the live fp8 row's own 0.90 GiB sits in 0.94 GiB,
        and 10% on a measured number would reject the engine that is running."""
        return self.cuda_graphs + int(abs(self.graphs_delta) * self.margin)

    def to_json(self) -> dict:
        g = lambda b: round(b / GIB, 4)
        return {"weights_gib": g(self.weights), "activation_gib": g(self.activation),
                "cuda_graphs_gib": g(self.cuda_graphs), "kv_gib": g(self.kv_bytes),
                "kv_tokens": self.kv_tokens, "kv_tokens_checked": self.kv_tokens_checked,
                "budget_gib": g(self.budget), "estimated": self.estimated,
                "basis": list(self.basis), "margin": self.margin,
                "assumptions": list(self.assumptions)}


@dataclass(frozen=True)
class Feasible:
    prediction: MemoryPrediction
    reason: str = ""


@dataclass(frozen=True)
class Infeasible:
    reason: str
    prediction: Optional[MemoryPrediction] = None


@dataclass(frozen=True)
class Unknown:
    """No measured basis for some term. Distinct from `vllm_startup.Unknown`
    (a parse that failed): this one is a prediction that cannot be made."""
    reason: str
    prediction: Optional[MemoryPrediction] = None


Feasibility = Union[Feasible, Infeasible, Unknown]


def _n_side(n: int) -> bool:
    # A launch with LoRA enabled carries fixed LoRA machinery that a launch
    # without it does not; a per-adapter delta measured between 1 and 2
    # adapters says nothing about 0 -> 1. Extrapolation never crosses zero.
    return n > 0


def predict_memory(state: CompositionState, c: Composition, *,
                   margin: float = DEFAULT_MARGIN) -> Union[MemoryPrediction, Unknown]:
    """Exact measured row, else additive from measured deltas, else Unknown.

    The additive model, term by term (all within one base and engine version):
    * weights / activation / CUDA graphs: an ANCHOR row (the measured row with
      the nearest adapter count at this slot rank and this batch cap), plus
      (n − n_anchor) × the per-adapter delta, plus (seqs − seqs_anchor) × the
      per-sequence delta. A delta is the difference between two measured rows
      that differ in that one count, divided by the count difference.
    * KV tokens per byte for (base, kv dtype): from any measured row with that
      dtype; the smallest ratio wins (conservative).
    * KV bytes = budget − weights − activation; graphs sit outside the budget.

    Two assumptions, recorded on the prediction when they are used:
    * `kv_dtype_independent`: a per-adapter delta may come from rows that also
      differ in KV dtype. KV dtype changes the KV pool, not weights or
      activation materially. On 2026-09-28 this is load-bearing: the only two
      rows (chips/bf16 and chips+jemm/fp8) differ in both.
    * `max_model_len_independent`: vLLM V1 profiles activation with
      `max_num_batched_tokens` (chunked prefill), not `max_model_len`, so a row
      at another context length is a valid anchor. Context enters only through
      the KV-tokens rule.
    """
    dev = state.device(c.device)
    facts = state.engine.get(c.device)
    ver = facts.engine_version if facts else ""
    key = c.key(state.adapters, ver)
    h = composition_hash(key)
    budget = dev.budget

    row = state.measured.get(h)
    if (isinstance(row, MeasuredCost) and abs(row.budget - budget) <= BUDGET_TOL
            and abs(row.gpu_fraction - dev.budget_fraction) < 1e-9):
        return MemoryPrediction(weights=row.weights_nontorch, activation=row.peak_activation,
                                cuda_graphs=row.cuda_graphs, kv_bytes=row.kv_bytes,
                                kv_tokens=row.kv_tokens, budget=row.budget, estimated=False,
                                basis=(h,))

    basis = sorted((hh, state.measured_keys[hh], m) for hh, m in state.measured.items()
                   if isinstance(m, MeasuredCost) and hh in state.measured_keys
                   and state.measured_keys[hh].base == c.base
                   and state.measured_keys[hh].engine_version == ver)
    if not basis:
        return Unknown("no_measured_basis:weights")

    def n_of(k: CompositionKey) -> int:
        return len(k.adapters)

    def rank_of(k: CompositionKey) -> int:
        return max((r for _, r in k.adapters), default=0)

    def dtype_of(k: CompositionKey) -> str:
        return k.kv_dtype or "auto"

    n, rank, seqs = c.max_loras, c.slot_rank(state.adapters), c.max_num_seqs
    assumptions = set()

    # per-adapter delta at this slot rank, batch cap held equal
    adapter_delta = None
    pairs = []
    for (h1, k1, m1), (h2, k2, m2) in itertools.combinations(basis, 2):
        if (n_of(k1) != n_of(k2) and n_of(k1) > 0 and n_of(k2) > 0
                and rank_of(k1) == rank_of(k2) == rank and k1.max_num_seqs == k2.max_num_seqs):
            d = n_of(k2) - n_of(k1)
            pairs.append(((m2.weights_nontorch - m1.weights_nontorch) / d,
                          (m2.peak_activation - m1.peak_activation) / d,
                          (m2.cuda_graphs - m1.cuda_graphs) / d,
                          dtype_of(k1) != dtype_of(k2), (h1, h2)))
    if pairs:
        # conservative: the largest delta seen for each term
        adapter_delta = tuple(max(p[i] for p in pairs) for i in range(3))
        adapter_pair_mixed = any(p[3] for p in pairs)
        adapter_pair_hashes = tuple(sorted({x for p in pairs for x in p[4]}))

    # per-sequence delta: rows equal in adapters, differing in batch cap
    seq_delta = None
    spairs = []
    for (h1, k1, m1), (h2, k2, m2) in itertools.combinations(basis, 2):
        if k1.adapters == k2.adapters and k1.max_num_seqs != k2.max_num_seqs:
            d = k2.max_num_seqs - k1.max_num_seqs
            spairs.append(((m2.peak_activation - m1.peak_activation) / d,
                           (m2.cuda_graphs - m1.cuda_graphs) / d, (h1, h2)))
    if spairs:
        seq_delta = tuple(max(p[i] for p in spairs) for i in range(2))
        seq_pair_hashes = tuple(sorted({x for p in spairs for x in p[2]}))

    anchors = [(hh, k, m) for hh, k, m in basis
               if _n_side(n_of(k)) == _n_side(n) and (n == 0 or rank_of(k) == rank)
               and (n_of(k) == n or adapter_delta is not None)
               and (k.max_num_seqs == seqs or seq_delta is not None)]
    if not anchors:
        if not any(_n_side(n_of(k)) == _n_side(n) and (n == 0 or rank_of(k) == rank)
                   for _, k, _ in basis):
            return Unknown(f"no_measured_basis:adapter_slot:{f'r{rank}' if n else 'lora_off'}")
        if not any(n_of(k) == n or adapter_delta is not None for _, k, _ in basis):
            return Unknown(f"no_measured_basis:adapter_delta:r{rank}")
        return Unknown("no_measured_basis:max_num_seqs")
    anchors.sort(key=lambda t: (abs(n_of(t[1]) - n), t[1].max_num_seqs != seqs,
                                dtype_of(t[1]) != c.kv_dtype,
                                t[1].max_model_len != c.max_model_len, t[0]))
    ah, ak, am = anchors[0]
    used = {ah}
    w, act, gr = float(am.weights_nontorch), float(am.peak_activation), float(am.cuda_graphs)
    gr0 = gr
    dn = n - n_of(ak)
    if dn:
        w += dn * adapter_delta[0]
        act += dn * adapter_delta[1]
        gr += dn * adapter_delta[2]
        used.update(adapter_pair_hashes)
        if adapter_pair_mixed:
            assumptions.add("kv_dtype_independent")
    ds = seqs - ak.max_num_seqs
    if ds:
        act += ds * seq_delta[0]
        gr += ds * seq_delta[1]
        used.update(seq_pair_hashes)
    if dtype_of(ak) != (c.kv_dtype or "auto"):
        assumptions.add("kv_dtype_independent")
    if ak.max_model_len != c.max_model_len:
        assumptions.add("max_model_len_independent")

    ratios = [(m.kv_tokens / m.kv_bytes, hh) for hh, k, m in basis
              if dtype_of(k) == (c.kv_dtype or "auto") and m.kv_bytes > 0]
    if not ratios:
        return Unknown(f"no_measured_basis:kv_tokens_per_byte:{c.kv_dtype or 'auto'}")
    tpb, rh = min(ratios)
    used.add(rh)

    kv_bytes = int(budget - w - act)
    return MemoryPrediction(
        weights=int(w), activation=int(act), cuda_graphs=int(gr), kv_bytes=kv_bytes,
        kv_tokens=int(math.floor(kv_bytes * tpb)) if kv_bytes > 0 else 0, budget=budget,
        estimated=True, basis=tuple(sorted(used)), margin=margin,
        assumptions=tuple(sorted(assumptions)), graphs_delta=int(gr - gr0))


def service_rates(state: CompositionState, c: Composition) -> Tuple[float, float]:
    """(prefill tok/s, decode tok/s per sequence) fitted for this base from the
    engine's own records (`replay_validate --fit-state`), or (0, 0): the
    replay then uses one blended rate, which ignores that prompt throughput is
    shared and over- or under-states load (see
    `_plans/composition-replay-validation.md`)."""
    best, at = (0.0, 0.0), -1.0
    for hh, m in state.measured.items():
        k = state.measured_keys.get(hh)
        if (isinstance(m, MeasuredCost) and k is not None and k.base == c.base
                and m.prefill_tok_s > 0 and m.decode_tok_s > 0 and m.measured_at > at):
            best, at = (m.prefill_tok_s, m.decode_tok_s), m.measured_at
    return best


def kv_paging(state: CompositionState, c: Composition) -> Tuple[int, float]:
    """(tokens per KV page, state pages per sequence) for this base and KV dtype,
    from measurements: the block size the engine printed for that dtype, and
    the per-sequence state the engine's own stats lines were fitted to. Either
    missing means (0, 0.0): the replay falls back to token accounting, which
    on a hybrid model undercounts the pool ~5x (see
    `_plans/composition-replay-validation.md`). The caller labels that."""
    block = state_pages = 0
    best_at = -1.0
    for hh, m in state.measured.items():
        k = state.measured_keys.get(hh)
        if not isinstance(m, MeasuredCost) or k is None or k.base != c.base:
            continue
        if m.state_pages_per_seq > 0:
            state_pages = max(state_pages, m.state_pages_per_seq)
        if (k.kv_dtype or "auto") == (c.kv_dtype or "auto") and m.block_size > 0 \
                and m.measured_at > best_at:
            block, best_at = m.block_size, m.measured_at
    if block <= 0 or state_pages <= 0:
        if state_pages > 0:
            # The base IS paged (its state was fitted) but this KV dtype's
            # block size was never measured. Token accounting here would price
            # this candidate ~5x too cheap against a paged live composition:
            # on 2026-09-30 that made a bf16 candidate look queue-free and the
            # composer recommend it. An unmeasured term is unknown, never 0.
            raise CostUnknown(f"no_measured_basis:kv_block:{c.kv_dtype or 'auto'}")
        return 0, 0.0
    return block, float(state_pages)


# --- feasibility -------------------------------------------------------------------

def feasible(state: CompositionState, c: Composition, *,
             margin: float = DEFAULT_MARGIN) -> Feasibility:
    """The five hard rules of design §5, plus HARD_PIN. Cheap rules first, so a
    candidate that is infeasible for a known reason is not reported `Unknown`
    merely because its memory has not been measured.

    Takes no cost weights. `margin` is a safety parameter recorded in the
    weights artifact but read only here, and it moves only estimated rows."""
    for dev, base in sorted(state.hard_pins):
        if dev == c.device and c.base != base:
            return Infeasible("hard_pin")
    for a in sorted(c.adapters):
        ad = state.adapters.get(a)
        if ad is None:
            return Infeasible(f"unknown_adapter:{a}")
        if ad.base != c.base:
            return Infeasible(f"adapter_base:{a}")
        if ad.rank > c.slot_rank(state.adapters):     # holds by construction today
            return Infeasible(f"adapter_rank:{a}")
    facts = state.engine.get(c.device)
    if facts is None or (c.kv_dtype or "auto") not in facts.kv_dtypes:
        return Infeasible(f"kv_dtype_unavailable:{c.kv_dtype}")
    too_long = 0
    for r in state.trace:
        t = rp.record_tokens(r)
        if t is not None and t > c.max_model_len and state.record_base(r) == c.base:
            too_long += 1
    if too_long:
        return Infeasible(f"truncates:{too_long}")

    p = predict_memory(state, c, margin=margin)
    if isinstance(p, Unknown):
        return p
    dev = state.device(c.device)
    if p.kv_bytes <= 0:
        return Infeasible("memory>budget", p)
    if p.cuda_graphs_checked > dev.capacity - p.budget:
        return Infeasible("cuda_graphs>outside_budget", p)
    if p.kv_tokens_checked < c.max_model_len:
        return Infeasible("kv_tokens<max_model_len", p)
    return Feasible(p)


# --- cost weights ------------------------------------------------------------------

@dataclass(frozen=True)
class Weights:
    version: int
    queue_s: float
    unserved: float
    swap_stall_s: float
    restart_downtime_s: float
    risk_prior_per_new_flag: float
    memory_margin: float
    hash: str = ""

    @classmethod
    def from_json(cls, obj: Mapping) -> "Weights":
        canon = json.dumps(obj, sort_keys=True, separators=(",", ":"))
        return cls(version=int(obj["version"]), queue_s=float(obj["queue_s"]),
                   unserved=float(obj["unserved"]), swap_stall_s=float(obj["swap_stall_s"]),
                   restart_downtime_s=float(obj["restart_downtime_s"]),
                   risk_prior_per_new_flag=float(obj["risk_prior_per_new_flag"]),
                   memory_margin=float(obj["memory_margin"]),
                   hash="sha256:" + hashlib.sha256(canon.encode()).hexdigest())


def load_weights(path) -> Weights:
    """The one I/O in this module. The hash is over canonical JSON, so
    whitespace edits do not change it and any value edit does."""
    with open(path) as f:
        return Weights.from_json(json.load(f))


# --- cost ---------------------------------------------------------------------------

class CostUnknown(Exception):
    """A cost term has no basis (no memory prediction, no service rate)."""


@dataclass(frozen=True)
class CostBreakdown:
    queue_s: float                  # mean over windows, request-seconds
    queue_s_worst: float
    swap_stalls: float              # swaps, mean over windows
    unserved: float                 # requests, mean over windows
    change_cost: float              # weighted already (it is priced in request-seconds)
    restart_requests: float         # downtime × expected rate
    new_flags: Tuple[str, ...]
    rate: float
    rate_basis: str
    self_traffic: int
    windows: int
    operating: float                # weighted queue + swaps + unserved (mean window)
    operating_worst: float
    total: float                    # operating + change_cost
    total_worst: float
    # Per device: "pages:<block>x<state>" or "tokens" (no measured paging facts:
    # queueing on a hybrid engine is then undercounted). Named so a reader of
    # the record knows which model priced it.
    kv_accounting: Tuple[str, ...] = ()

    def to_json(self) -> dict:
        r = lambda x: round(x, 6)
        return {"queue_s": r(self.queue_s), "queue_s_worst": r(self.queue_s_worst),
                "swap_stalls": r(self.swap_stalls), "unserved": r(self.unserved),
                "change_cost": r(self.change_cost), "restart_requests": r(self.restart_requests),
                "new_flags": list(self.new_flags), "rate": r(self.rate),
                "rate_basis": self.rate_basis, "self_traffic": self.self_traffic,
                "windows": self.windows, "operating": r(self.operating),
                "operating_worst": r(self.operating_worst), "total": r(self.total),
                "total_worst": r(self.total_worst), "kv_accounting": list(self.kv_accounting)}


def _route(state: CompositionState, assignment: Mapping[str, Composition],
           r: Mapping) -> Optional[str]:
    """The device that would serve a record, or None. Lowest device id wins a
    tie; load balancing across equal units is not modelled."""
    base = state.record_base(r)
    a = r.get("adapter")
    for dev in sorted(assignment):
        c = assignment[dev]
        if base is not None and c.base == base and (not a or a in c.adapters):
            return dev
    return None


def cost(state: CompositionState, assignment: Mapping[str, Composition], weights: Weights, *,
         predictions: Optional[Mapping[str, MemoryPrediction]] = None) -> CostBreakdown:
    """Weighted cost of running `assignment` (device -> composition) against the
    trace, per design §5 and §10 decision 4. Raises `CostUnknown` when a term
    cannot be computed — a cost is never quietly 0.

    Self traffic occupies the KV pool in the replay (it is served) but its delay,
    its unserved count and its rate are excluded."""
    preds = dict(predictions or {})
    for dev, c in assignment.items():
        if dev not in preds:
            p = predict_memory(state, c, margin=weights.memory_margin)
            if isinstance(p, Unknown):
                raise CostUnknown(f"{dev}:{p.reason}")
            preds[dev] = p
    rates: Dict[str, float] = {}
    by_base: Dict[str, list] = {}
    for r in state.trace:
        b = state.record_base(r)
        if b is not None:
            by_base.setdefault(b, []).append(r)

    def window_cost(records: Sequence[Mapping]) -> Tuple[float, float, float]:
        routed: Dict[str, list] = {}
        unserved = 0
        for r in records:
            dev = _route(state, assignment, r)
            if dev is None:
                unserved += 0 if is_self_traffic(r) else 1
            else:
                routed.setdefault(dev, []).append(r)
        q = swaps = 0.0
        for dev in sorted(routed):
            c = assignment[dev]
            if c.base not in rates:
                rate = rp.fit_rate(by_base.get(c.base, ()))
                if rate is None:
                    raise CostUnknown(f"no_service_rate:{c.base}")
                rates[c.base] = rate
            pre, dec = service_rates(state, c)
            jobs = rp.jobs_from_records(routed[dev], rates[c.base],
                                        counted=lambda r: not is_self_traffic(r),
                                        prefill_tok_s=pre, decode_tok_s=dec)
            block, spages = kv_paging(state, c)
            res = rp.replay(jobs, kv_tokens=preds[dev].kv_tokens, max_num_seqs=c.max_num_seqs,
                            max_loras=c.max_loras, block_size=block, state_pages=spages,
                            prefill_tok_s=pre)
            q += res.total_queue_s
            swaps += res.swaps
        return q, swaps, float(unserved)

    per = [window_cost(rp.in_window(state.trace, w)) for w in state.windows] \
        if state.windows else [window_cost(state.trace)]
    op = [weights.queue_s * q + weights.swap_stall_s * s + weights.unserved * u
          for q, s, u in per]
    mean = lambda i: sum(p[i] for p in per) / len(per)

    # change cost: every device whose composition differs from live pays the
    # restart (downtime × expected rate of the demand it serves now × multiplier)
    # plus the risk prior for each launch flag its engine has never started with.
    change = restart_reqs = rate_total = 0.0
    basis = "none"
    new_flags: List[str] = []
    for dev in sorted(assignment):
        c, live = assignment[dev], state.live.get(dev)
        if live == c:
            continue
        served = [r for r in state.trace if not is_self_traffic(r)
                  and live is not None and _route(state, {dev: live}, r) == dev]
        rate, basis = rp.expected_rate(served, state.now)
        rate_total += rate
        restart_reqs += weights.restart_downtime_s * rate
        facts = state.engine.get(dev)
        started = facts.flags_started if facts else frozenset()
        fresh = sorted(launch_flags(c) - started)
        new_flags += [f"{dev}:{f}" for f in fresh]
        # Same unit as everything else (request-seconds): every request that
        # arrives while the engine restarts goes unserved and is charged as one.
        change += (weights.restart_downtime_s * rate * weights.unserved
                   + weights.risk_prior_per_new_flag * len(fresh))
    self_n = sum(1 for r in state.trace if is_self_traffic(r))
    return CostBreakdown(
        queue_s=mean(0), queue_s_worst=max(p[0] for p in per), swap_stalls=mean(1),
        unserved=mean(2), change_cost=change, restart_requests=restart_reqs,
        new_flags=tuple(new_flags), rate=rate_total, rate_basis=basis, self_traffic=self_n,
        windows=len(per), operating=sum(op) / len(op), operating_worst=max(op),
        total=sum(op) / len(op) + change, total_worst=max(op) + change,
        kv_accounting=tuple(f"{dev}:" + (f"pages:{b}x{sp:g}" if b else "tokens")
                            + (f",service:{pr:.0f}/{de:.1f}" if pr else ",service:blended")
                            for dev in sorted(assignment)
                            for b, sp in [kv_paging(state, assignment[dev])]
                            for pr, de in [service_rates(state, assignment[dev])]))


# --- deciders -----------------------------------------------------------------------

class Composer(Protocol):
    name: str
    version: str

    def propose(self, state: CompositionState) -> List[Composition]: ...


class CompositionSpaceTooLarge(Exception):
    pass


class ExhaustiveComposer:
    """Every composition the search space allows, per device.

    Size = Σ_device Σ_base 2^(adapters of that base) × |kv dtypes| ×
    |max_model_len values| × |max_num_seqs values|. Today (one card, one base
    with two adapters, 2 dtypes, 3 lengths, 3 caps) that is 4 × 18 = 72;
    design §5 puts the realistic catalogue at ~10³. Past `MAX_CANDIDATES` it
    refuses rather than quietly sampling: a space that large needs a real
    proposer, not a longer wait.

    One device changes per candidate; the others stay live. A joint move
    (swapping bases between two cards) is not proposed in v1."""
    name = "exhaustive"
    version = "1"

    def __init__(self, max_candidates: int = MAX_CANDIDATES) -> None:
        self.max_candidates = max_candidates

    def propose(self, state: CompositionState) -> List[Composition]:
        s = state.search
        by_base = {b: sorted(a for a, ad in state.adapters.items() if ad.base == b)
                   for b in state.bases}
        per_combo = len(s.kv_dtypes) * len(s.max_model_lens) * len(s.max_num_seqs)
        size = len(state.devices) * sum(2 ** len(v) for v in by_base.values()) * per_combo
        if size > self.max_candidates:
            raise CompositionSpaceTooLarge(f"{size} candidates > {self.max_candidates}")
        out = []
        for d in state.devices:
            for b in state.bases:
                ads = by_base[b]
                for k in range(len(ads) + 1):
                    for subset in itertools.combinations(ads, k):
                        for kv, ln, sq in itertools.product(s.kv_dtypes, s.max_model_lens,
                                                            s.max_num_seqs):
                            out.append(Composition(d.id, b, frozenset(subset), kv, ln, sq))
        return out


# --- the run and its record -----------------------------------------------------------

@dataclass(frozen=True)
class CandidateRow:
    device: str
    hash: str
    composition: Composition
    feasibility: str                # feasible | infeasible | unknown
    reason: str
    outcome: str                    # chosen | ranked | filtered
    live: bool = False
    cost: Optional[CostBreakdown] = None
    prediction: Optional[MemoryPrediction] = None

    def to_json(self) -> dict:
        return {"device": self.device, "hash": self.hash,
                "composition": self.composition.to_json(), "feasibility": self.feasibility,
                "reason": self.reason, "outcome": self.outcome, "live": self.live,
                "cost": self.cost.to_json() if self.cost else None,
                "prediction": self.prediction.to_json() if self.prediction else None}


@dataclass(frozen=True)
class CompositionDecision:
    """Ready to become ONE ledger Decision (design §8). `chosen` is the chosen
    candidate's composition, or None for keep."""
    candidates: Tuple[CandidateRow, ...]
    chosen: Optional[Composition]
    chosen_hash: str                # "keep" or the chosen composition hash
    reason: str
    policy: Mapping[str, str]
    candidates_total: int
    filtered: Mapping[str, int]     # reason -> count, over ALL candidates (incl. dropped)
    emitter: str = "composition"
    decision: str = "compose"

    def to_json(self) -> dict:
        return {"emitter": self.emitter, "decision": self.decision,
                "chosen": self.chosen.to_json() if self.chosen else "keep",
                "chosen_hash": self.chosen_hash, "reason": self.reason,
                "policy": dict(self.policy), "candidates_total": self.candidates_total,
                "filtered": dict(sorted(self.filtered.items())),
                "candidates": [c.to_json() for c in self.candidates]}


def _row_reason(f: Feasibility) -> Tuple[str, str]:
    if isinstance(f, Feasible):
        return "feasible", ""
    if isinstance(f, Unknown):
        return "unknown", f"unknown:{f.reason}"
    if f.reason == "hard_pin":
        return "infeasible", "filtered:hard_pin"
    return "infeasible", f"filtered:infeasible:{f.reason}"


def run_composition(state: CompositionState, composer: Composer,
                    weights: Weights) -> CompositionDecision:
    """Score every proposal and the live composition with the same `feasible`
    and `cost`; choose the cheapest feasible proposal only if it beats live by
    more than its change cost, else `keep`. Deterministic: ties break toward
    the fewest changed fields, then the longer context, then the hash. Writes
    nothing and applies nothing."""
    live_assign = dict(state.live)
    ver = lambda dev: state.engine[dev].engine_version if dev in state.engine else ""
    hash_of = lambda c: composition_hash(c.key(state.adapters, ver(c.device)))

    live_cost: Optional[CostBreakdown] = None
    live_err = ""
    try:
        live_cost = cost(state, live_assign, weights)
    except CostUnknown as e:
        live_err = str(e)

    seen = set()
    rows: List[CandidateRow] = []
    proposals = [(c, True) for _, c in sorted(state.live.items())] + \
        [(c, False) for c in composer.propose(state)]
    for c, is_live in proposals:
        h = hash_of(c)
        if (c.device, h) in seen:
            continue
        seen.add((c.device, h))
        is_live = is_live or state.live.get(c.device) == c
        f = feasible(state, c, margin=weights.memory_margin)
        feas, reason = _row_reason(f)
        pred = f.prediction
        cb = None
        if is_live:
            cb = live_cost
            if cb is None and feas == "feasible":
                feas, reason = "unknown", f"unknown:cost:{live_err}"
        elif feas == "feasible":
            try:
                cb = cost(state, {**live_assign, c.device: c}, weights,
                          predictions={c.device: pred})
            except CostUnknown as e:
                feas, reason = "unknown", f"unknown:cost:{e}"
        rows.append(CandidateRow(device=c.device, hash=h, composition=c, feasibility=feas,
                                 reason=reason, outcome="filtered" if reason else "ranked",
                                 live=is_live, cost=cb, prediction=pred))

    # TIES BREAK TOWARD THE SMALLEST CHANGE, then the longer context, and
    # only then the hash. A hash tie-break once picked a 16k-context bf16
    # composition over the 24k fp8 one on a trace too light to tell them apart:
    # equal cost, and the arbitrary winner quietly shortened every caller's
    # window. When the evidence cannot separate two candidates, the one that
    # disturbs less and takes away less wins.
    def _moved(c: Composition) -> int:
        live = state.live.get(c.device)
        if live is None:
            return 5
        return sum((live.base != c.base, live.adapters != c.adapters,
                    live.kv_dtype != c.kv_dtype, live.max_model_len != c.max_model_len,
                    live.max_num_seqs != c.max_num_seqs))
    best = min((r for r in rows if not r.live and r.feasibility == "feasible" and r.cost),
               key=lambda r: (round(r.cost.total, 6), _moved(r.composition),
                              -r.composition.max_model_len, r.hash, r.device), default=None)
    if live_cost is None:
        chosen, why = None, f"keep: live composition unscored ({live_err})"
    elif best is None:
        chosen, why = None, "keep: no feasible scored candidate"
    elif best.cost.total < live_cost.total - 1e-9:
        chosen = best
        why = (f"gain {live_cost.operating - best.cost.operating:.3f} > change_cost "
               f"{best.cost.change_cost:.3f}")
    else:
        chosen = None
        why = (f"keep: best gain {live_cost.operating - best.cost.operating:.3f} <= "
               f"change_cost {best.cost.change_cost:.3f}")

    final = []
    for r in rows:
        if chosen is not None and r is chosen:
            out = "chosen"
        elif chosen is None and r.live:
            out = "chosen"
        elif r.reason:
            out = "filtered"
        else:
            out = "ranked"
        final.append(CandidateRow(r.device, r.hash, r.composition, r.feasibility,
                                  r.reason or ("live" if r.live else ""), out, r.live,
                                  r.cost, r.prediction))
    filtered = Counter(r.reason for r in final if r.outcome == "filtered")

    final.sort(key=lambda r: (r.cost is None, r.cost.total if r.cost else 0.0, r.hash,
                              r.device))
    kept = final[:LEDGER_CANDIDATE_CAP]
    must = [r for r in final if (r.live or r.outcome == "chosen") and r not in kept]
    if must:
        kept = kept[:LEDGER_CANDIDATE_CAP - len(must)] + must

    return CompositionDecision(
        candidates=tuple(kept), chosen=chosen.composition if chosen else None,
        chosen_hash=chosen.hash if chosen else "keep", reason=why,
        policy={"composer": composer.name, "composer_version": composer.version,
                "weights": weights.hash},
        candidates_total=len(final), filtered=dict(filtered))
