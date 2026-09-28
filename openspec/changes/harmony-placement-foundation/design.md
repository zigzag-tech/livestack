# Design — harmony-placement-foundation

Read first:
- `_plans/resource-planner.md` (the residency brain this sits above)
- `_plans/decision-ledger.md` (the record every decision leaves)
- `openspec/changes/scheduler-policy-routine/design.md` §8 and §10 (rules for follow-up
  families; the replay prerequisite this change supplies)
- `~/jingway/openspec/specs/weaving/spec.md` (the actuation shape §7 prepares for)
- `HARMONY.md` §"Operating it"

The worked example throughout is the 2026-09-28 recomposition of `llm_general` on
xc-tower-ubuntu GPU 1 (RTX 3090, 23.56 GiB). vLLM's startup lines, before → after:

| | chips only, bf16 KV | chips + jemm, fp8 KV |
|---|---|---|
| weights + non-torch | 18.18 GiB | 18.39 GiB |
| peak activation | 2.23 GiB | 2.56 GiB |
| CUDA graphs (outside the 0.96 budget) | 0.46 GiB | 0.90 GiB |
| KV cache | 2.21 GiB = 29,749 tokens | 1.66 GiB = 37,981 tokens |
| max concurrency at 24,576 tokens | 1.21x | 1.55x |

## 1. Ownership and ids

| Durable state | Owner process | Where | Bound |
|---|---|---|---|
| Units file (the live composition) | a person; harmony-llm reads it | `/etc/harmony/llm-units*.json` | n/a (config) |
| Measured unit cost | harmony-llm (parses its own vLLM) | in memory + `/residence`; last measurement per composition hash persisted in `~/.cache/livestack/unit-costs.jsonl` | one row per composition hash, 256 rows, oldest dropped |
| Demand log | harmony-llm (proxy path) | `~/.cache/livestack/demand/<unit>.jsonl*` | size × files **and** age; §3 |
| WorldState snapshots | HostBroker | `~/.cache/livestack/snapshots/<sha256>.json.zst` | age window matching the ledger's, plus a byte cap; §4 |
| Composition decisions + outcomes | HostBroker | the existing JSONL ledger (`ledger.py`) | the ledger's existing bound |
| Cost-weights artifact | a person (via review) | `node-py/livestack_node/composition_weights/v<N>.json`, content hash `sha256:` | versioned in git |

Ids that cross a boundary:
- `composition_hash`: sha256 of the canonical JSON of one device's `Composition`
  (base, sorted adapters with ranks, kv dtype, max context, batch cap, engine version).
  harmony-llm computes it at launch and stamps it on every measured-cost row and
  demand-log record. The HostBroker recomputes it from the proposal, so a measurement
  joins the proposal that caused it without trusting either side's bookkeeping.
- `decision_id`: the ledger's monotonic ULID. A composition decision's outcome rows
  carry it as `parent_decision_id`.
- `snapshot`: `sha256:` of the canonical `WorldState`. Identical states dedupe to one
  file.

## 2. Measured unit cost

harmony-llm already waits for vLLM's `/v1/models` before reporting a unit ready. At that
point it parses the engine's own startup lines. It does not estimate.

- `Model loading took X GiB`
- `Available KV cache memory: X GiB`
- `GPU KV cache size: N tokens, Maximum concurrency for L tokens per request: Cx`
- `Graph capturing finished … took X GiB`
- `Actual usage is A GiB for consumed memory …, B GiB for peak activation, and C GiB for CUDAGraph memory`

These become a `MeasuredCost{weights_nontorch, peak_activation, cuda_graphs, kv_bytes,
kv_tokens, max_concurrency_at, budget_bytes, engine_version, composition_hash,
measured_at, source: "vllm-startup"}` reported on `/residence`. `Unit.footprint`
becomes the measured `weights_nontorch + peak_activation + cuda_graphs`, and
`activation_headroom` stops being hand-set for units that have a measurement.

Rules:
- **A declared `footprint_gb` is a prior.** It is used until a measurement for the
  current `composition_hash` exists, and it is marked `source: "declared"` wherever it
  appears.
- **A parse failure is `unknown`, not 0 and not the prior.** An engine that became ready
  but whose memory lines did not parse reports `measured: unknown` with the unmatched
  line names. This is counted, logged and visible on `/residence`. The planner then
  treats that unit's footprint as the whole device budget, the conservative envelope,
  until it is re-measured.
- **The parser is pinned to vLLM versions it has seen.** An unrecognised engine version
  still attempts the parse and reports what matched. A version-specific fixture per
  supported release lives in the tests.

## 3. Inference demand log

One record per request the proxy forwards:

```
{ts, unit, composition_hash, adapter|null, owner_ns, requirement_hash,
 prompt_tokens|null, completion_tokens|null, elapsed_ms, queue_ms|null,
 outcome: ok|refused|unsatisfied|transport|timeout, http_status}
```

- **Token counts come from the upstream response's `usage`.** Streamed responses without
  it record `null`, never 0. `/v1/classifier` records the compiled branch's
  `usage.input_tokens`.
- **`queue_ms`** is `null` until vLLM exposes per-request queue time to the proxy. This
  change does not scrape it from metrics, and the gap is named in `HARMONY.md`.
- **`owner_ns`** is the principal namespace only (`benchday:`, `attune:`, …), not the
  owner id. Demand modelling needs to know which application sent a request, not which
  person. This keeps the log outside the privacy exemptions list.
- **Bound:** rotate at 64 MiB, keep 8 files, and drop records older than 21 days. An
  unset age window fails closed (the log refuses to start rather than grow unbounded),
  matching the ledger's rule for a bound that deletes.
- **Writes are off the request path.** The writer is a bounded in-memory queue drained
  by one task. When the queue is full, the record is dropped and a counter increments,
  which `/residence` reports. A dropped record is never silent, and a slow disk never
  slows a request.

Twenty-one days is three weekly cycles, the minimum for §6's time-of-day and weekday
comparison.

## 4. Replayable `plan()` records

`HostBroker._emit_plan` today writes one record per action, with candidate rows that
carry priority, tier and residency but no footprints or capacities.
`scheduler-policy-routine` §10 names that gap as the blocker for tuning placement. This
change adds:

- The canonical `WorldState` as it existed when `plan()` ran, written once per distinct
  state to the snapshot store and referenced from every action record of that plan as
  `snapshot: "sha256:…"`. It includes devices, capacities, `measured_free`, units with
  footprints and their `source`, placements, requests, demand and `now`.
- A replay check. `plan(load(snapshot), policy)` must reproduce the recorded actions
  exactly. Mismatches are counted and surfaced, as the scheduler cutover does
  ("5006 decisions, mismatches 0").
- **Bound:** snapshots are compressed and deduped by hash. The reconcile loop re-plans
  every 5 s, but most ticks see an unchanged state and dedupe to an existing file. The
  store keeps the ledger's age window plus a 512 MiB cap, oldest first.

## 5. The composition problem

`composition.py` is pure: no I/O, no clock, no inference, following the planner's
convention.

**State** (`CompositionState`):
- devices (capacity, budget fraction, labels)
- the adapter catalogue (name, base model, rank, target modules, path hash)
- base models (name, family, quantisation, vision)
- measured costs keyed by `composition_hash`
- a demand trace (a window of demand-log records)
- the live composition per device
- engine facts per host (vLLM version, available KV dtypes, and known-broken flags such
  as "fp8 KV needs the gcc-14 drop-in", recorded as data)

**Decision variable** (`Composition`, one per device):
`{base, adapters: frozenset, kv_dtype, max_model_len, max_num_seqs}`. `max_loras` is
derived as `len(adapters)`, as harmony-llm already does. The search space is the product
over devices of the allowed values. With today's catalogue that is on the order of 10³
and exhaustible in milliseconds.

**Feasibility** (`feasible(state, c) -> Feasible | Infeasible(reason) | Unknown(reason)`),
evaluated in code and never tunable:
1. The predicted memory fits the device budget, with CUDA graphs checked against the
   space outside it.
2. The predicted KV tokens are at least `max_model_len`. vLLM refuses to start below
   this, and it is the rule the 2026-09-28 bf16 attempt would have broken.
3. Every adapter's base equals `c.base`, and its rank is at most the slot rank.
4. The KV dtype is available on that host's engine, per the engine facts.
5. `max_model_len` covers the longest prompt in the demand trace for that base. Otherwise
   the result is infeasible with reason `truncates:<n> requests`, not silently
   feasible.

**Predicting memory.** An exact measured row for this `composition_hash` wins. Otherwise
the prediction is additive from measured deltas: base weights, then per-adapter slot cost
at that rank, then the activation and CUDA-graph increments of `len(adapters)` and
`max_num_seqs`, then KV tokens per GiB for that base and dtype. The result is marked
`estimated` and carries a safety margin (a weights-artifact parameter, starting at 10%).
A composition with no measured neighbour for some term returns `Unknown`, never
`Feasible`. The table at the top of this document is the first calibration fixture:
- chips-only bf16 must predict the measured row exactly;
- chips + jemm bf16 must predict **infeasible** (about 22k tokens, below 24,576);
- chips + jemm fp8 must predict feasible within margin.

**Cost** (`cost(state, c, weights) -> CostBreakdown`), a weighted sum where every term is
reported separately:
- `queue_s`: total queueing delay from replaying the trace through §6's admission model.
- `swap_stalls`: adapter loads the trace forces when more adapters are demanded than
  `max_loras` holds, weighted by how often they alternate.
- `unserved`: trace requests whose base or adapter has no unit on this host, which would
  go elsewhere or be refused.
- `change_cost`: expected restart downtime multiplied by the demand rate in the apply
  window, plus a risk prior for any flag this host has never started with (as fp8 KV had
  not, on 2026-09-28).

The weights are a versioned artifact. v1 is hand-set, recorded, and reviewed like code.

## 6. The replay admission model

`composition_replay.py` models only what decides queueing on vLLM:
- a KV token pool of size `kv_tokens`;
- a batch cap `max_num_seqs`;
- `max_loras` adapter slots.

Requests arrive at their logged `ts` and hold `prompt + completion` tokens for a service
time, taken from the logged `elapsed_ms` of requests of similar length on the same base.
It is a model, so it is validated before it is trusted. Replayed against 7 days of the
live composition, its running and waiting counts must match the journal's 10-second
`Running/Waiting` samples within a tolerance recorded in the tests. The known shape it
must reproduce: never more than 11 running, queueing at 91–100% KV.

Demand for the next window is estimated from the trace by two comparisons: an
exponentially decayed recent rate, and "same hour last week". A proposal is scored
against several past windows, not one point forecast, and the reported cost is the
worst window alongside the mean.

## 7. The decider interface, and what plugs in later

```python
class Composer(Protocol):
    name: str
    version: str
    def propose(self, state: CompositionState) -> list[Composition]: ...
```

`run_composition(state, composer, weights)` calls `propose`, and runs `feasible` and
`cost` on **every** candidate, including the live composition as a baseline. It then
chooses the lowest-cost feasible candidate, but only if it beats the live one by more
than `change_cost`. Otherwise it chooses "keep". It writes one ledger decision (§8). The
decider never sees the ledger and never applies anything.

- **Today:** `ExhaustiveComposer`, which enumerates the whole space.
- **Woven actuation (follow-up change).** Applying a chosen composition becomes a fleetd
  `routine` of `WeaveStep`s:
  1. write the units file, `effect: reversible`, with the backup as the inverse;
  2. restart, `effect: irreversible`;
  3. verify, with gates: the engine becomes ready within its deadline; measured memory is
     within margin of the prediction; KV tokens ≥ `max_model_len`; the first N demand
     records are `ok`.
  
  A gate that stays red escalates to a repair turn with the engine log. The fp8/gcc-15
  failure is exactly that case. The fallback is the recorded backup. This change
  specifies those gates as requirements (see spec `unit-composition`) so the follow-up
  only has to implement them.
- **A learned proposer ("System 1", follow-up).** Trained on the §8 decision/outcome
  rows, it implements `Composer` and returns candidates in milliseconds. It gains no
  authority: its candidates go through the same `feasible` and `cost`, it is evaluated by
  §6's replay on held-out windows before it may run in shadow, and promotion needs a
  person.

## 8. The record, and the relation to Jingway's compiled policy

A composition run is one ledger `Decision`:
- `emitter: "composition"`, `decision: "compose"`;
- `candidates`: each composition with its hash, its feasibility result and reason, and
  its cost breakdown, capped at the ledger's 64 by lowest cost, with the live composition
  always kept;
- `chosen` and `reason`;
- `policy: {composer, composer_version, weights: "sha256:…"}` and `snapshot`.

Outcomes are joined later by `parent_decision_id`:
- `outcome: measured`, the first `MeasuredCost` for the chosen `composition_hash` with
  predicted-vs-measured per term;
- `outcome: served`, per hour for the first 24 h after apply: ok/refused counts, p50/p95
  elapsed and replay-vs-actual queueing;
- `outcome: not_applied` after 7 days if no measurement for that hash ever appears, so
  an ignored proposal is distinguishable from a pending one.

The record shape follows the Jingway `policy_decision`/`policy_outcome` split (rows,
chosen, propensity, outcomes joined by id). An exhaustive composer's propensity is 1 for
the chosen row. It is not a `PolicyFamily` in this change: a family's `evaluate` is a
microsecond pure scoring of given rows, while composition is a combinatorial search over
seconds-scale state. §10 asks whether it should become one.

`scheduler-policy-routine` §8 rules are respected:
- a unit marked `HARD_PIN` is never absent from a proposed composition (`filtered:hard_pin`,
  in code);
- composition never infers;
- Jingway-principal demand (`owner_ns: jingway`) is recorded but tagged `self_traffic` and
  excluded from cost.

## 9. harmony-llm's source of truth

`~/harmony-llm/server.py` is deployed from a directory that is not a git repository.
Tasks 2 and 3 edit it. Before they start, it needs a home: either vendored into this repo
under `harmony-llm/` or given its own repo. Until then, every change to it is
unreviewable and every host's copy may differ. This is §10's first question.

## 10. Open questions for a person (ask; do not decide)

1. Where should harmony-llm's source live: vendored into livestack, or its own repo?
   Tasks 2–3 are blocked on this.
2. Should composition become a Jingway `PolicyFamily` later, or stay a sibling that
   shares only the record format and the replay tooling? This change assumes a sibling.
3. Are 21 days of demand-log retention, and namespace-only owner identity, acceptable?
4. Initial cost weights: how much is one minute of `llm_general` downtime worth against
   one second of queueing, summed over a day?
5. Should `max_num_seqs` and `kv_dtype` be in the search space from day one? Including
   them is what would have found fp8, and it is also what would have proposed the
   compile-trap flag before the engine-facts row existed.
