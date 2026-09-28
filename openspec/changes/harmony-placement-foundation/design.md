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
| WorldState snapshots | HostBroker | `~/.cache/livestack/snapshots/decisions-<host>/<sha256>.json.gz` | age window matching the ledger's, plus a 512 MiB cap; §4 |
| Composition decisions + outcomes | the composer CLI (`compose.py`), the only writer | `~/.cache/livestack/composition-<host>.jsonl`, same `JsonlLedger` writer and bounds | 8 MiB × 4 files, the ledger's age window |
| Composition inputs (node facts) | the composer CLI | `~/.cache/livestack/snapshots/composition-<host>/` | as WorldState snapshots |
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
It is a model, so it is validated before it is trusted. Replayed against the live
composition's demand log, its running and waiting counts must match the journal's
10-second `Running/Waiting` samples within a tolerance recorded in the tests. That
end-to-end check needs the demand log, which did not exist before this change. It is a
skipped test that names that input, not a pass. What the journal already shows (about
two days, fixture `tests/fixtures/composition/journal-llm_general-2026-09-28.json`):
never more than 11 running, and requests waiting at **every** KV-usage level, most often
80–90%. The v1 model reserves prompt + completion at admission, so it can only queue
when the pool is ≥ ~91% full. It therefore under-predicts queueing below a full cache.
That gap is pinned by a strict expected-failure test, so a model change that closes it
is noticed.

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
- `request: {composer, composer_version, weights: "sha256:…", candidates_total,
  filtered}` and `snapshot`. `request` rather than `policy`: the ledger schema reserves
  `policy` for the scheduler's compiled-policy pointer and closes it.
- Each candidate row carries its composition, feasibility, prediction and cost
  breakdown in `detail`: values, not references.

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

## 8a. What implementation changed (2026-09-28)

- **The composer is a CLI, not a host-broker route.** `livestack-hostd` on
  xc-tower-ubuntu runs a pinned release directory (`warm-for-kind-978cfa1e`) that is
  far behind `main`: `main` has since gained the meshlink backbone and the policy
  runtime. Adding the route would have meant shipping all of that unrelated work to
  the residency authority. The CLI is also the ledger's only writer, so there is no
  multi-process rotation race. A later broker release can call `compose.propose()`
  from a route.
- **harmony-llm serves `GET /composition/facts`** (read-only): live units as
  launched, measured rows, the adapter catalogue on disk, engine KV dtypes
  (`HARMONY_KV_DTYPES`), and the demand trace. Card size comes from `nvidia-smi`,
  never torch: a torch CUDA call would create a context and take VRAM from a card
  with about 1 GiB free.
- **Adapters on disk count.** On 2026-09-28 JEMM was only a directory. A unit may
  declare `lora_base` (the unquantized model it is a quantization of, as adapters'
  `base_model_name_or_path` names it). Catalogue adapters with that base are
  candidates for the unit.
- **The change cost is in request-seconds.** It is restart downtime × expected rate ×
  `unserved` (30), plus 600 per never-started flag. The first version multiplied by a
  unitless 3.
- **Ties break toward the smaller change, then the longer context, then the hash.**
  A hash tie-break once chose a 16k-context bf16 composition over 24k fp8 on a trace
  too light to separate them.
- **Snapshots are gzip, not zstd.** The package is stdlib-only.

## 8b. Why the measurement is not yet the admission number (2026-09-28)

The first deploy made harmony-llm report the measured 25.24e9 B as `llm_general`'s
`footprint`. The broker sizes card `a46c4c2e` at its measured 25.30e9 B minus the
default 2 GB reserve (`LIVESTACK_RESERVED_GB`), which leaves about 23.3e9 B. The unit
stayed resident, but any reload through admission would have been unplaceable.
Reverted the same hour. Two things were wrong at once:

- **The full measured footprint is not a requirement.** vLLM sizes its KV pool to fill
  the budget it is given. What it cannot run without is weights + activation + CUDA
  graphs + KV for one request of its `max_model_len`: `MeasuredCost.min_footprint`,
  about 24.6e9 B here.
- **The device reserve double-counts.** It exists (`planner.Device` docstring) to
  cover activation memory that declared, weights-only footprints omit. A measured
  footprint already contains the activation. Even the minimum, 24.6e9 B, plus the
  2 GB reserve exceeds the card.

So `footprint` stays the declared prior (`footprint_source: declared`), and the
measurement is reported beside it for composition. Adopting it for admission needs
a planner change: no device reserve for units whose footprint is measured, or a
per-unit reserve. That is its own change, and task 2.2 stays open until it lands.

## 9. harmony-llm's source of truth

`~/harmony-llm/server.py` is a symlink to this repo's
`node-py/examples/harmony-llm/server.py`, and production on xc-tower-ubuntu (the only
host running `harmony-llm.service`) executes the `~/livestack` main checkout directly.
So it is already version-controlled, and **merging to `main` in that checkout is a
deploy that takes effect at the next restart**. New logic goes in `livestack_node`
modules (`vllm_startup.py`, `demand_log.py`) that `server.py` imports, which keeps
the 1,763-line server from growing further and makes the logic testable without it.

## 10. Decisions (2026-09-28)

The owner asked for the recommended answer to each open question:

1. **Source of truth:** already in this repo (§9). No move needed.
2. **Compiled policy:** composition is a **sibling** of Jingway's `PolicyFamily`. It
   shares the decision/outcome record shape and replay discipline, not the Rust family
   contract. Revisit once a learned proposer exists and needs OPE over its propensities.
3. **Demand log:** 21-day retention, principal namespace only, as specified in §3.
4. **Cost weights v1**, in request-seconds of delay (hand-set, reviewed like code):
   `queue_s` = 1.0 per request-second queued; `unserved` = 30 per request with no
   serving unit on the host; `swap_stall` = 0.5 s × alternations (a prior until swaps
   are measured); `change_cost` = restart downtime (measured: 125–145 s for
   `llm_general`) × expected request rate in the apply window × 3, plus a risk prior of
   600 for each launch flag the host has never started with.
5. **Search space:** `kv_dtype` and `max_num_seqs` are in it from day one.
   Availability is gated by per-host engine facts (for example, fp8 KV requires the
   `NVCC_PREPEND_FLAGS` gcc-14 drop-in on xc-tower-ubuntu), and a never-started flag
   pays the risk prior above.
