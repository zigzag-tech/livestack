## Why

Every GPU unit Harmony serves is hand-composed today. A person decides which base model
runs on which card, which LoRA adapters ride on it, and which KV dtype, context length
and batch cap it starts with, then writes those into `/etc/harmony/llm-units.json`. The
planner (`planner.plan()`) only loads and evicts the units it is given; it has no idea
what a unit *could* have been. Nothing records what a composition actually cost or how
it served.

On 2026-09-28 the `llm_general` unit on xc-tower-ubuntu (one RTX 3090) was recomposed by
hand to serve a second adapter (`jemm`) beside `chips-settinghead-v1`. That one edit hit
every gap this change closes:

- **Declared cost was wrong by 2x.** The estimate for a second rank-16 adapter was
  ~0.45 GiB. vLLM's own startup lines said ~1 GiB: weights +0.21, peak activation +0.33,
  CUDA graphs +0.44 GiB. The unit still declares `footprint_gb: 21`, which is now false
  (the budget alone is 22.62 GiB). Harmony parses none of those lines.
- **The feasibility rule lived in a person's head.** With bf16 KV the second adapter
  leaves less than one 24,576-token request of KV cache, and vLLM refuses to start. fp8 KV
  made it fit (29,749 → 37,981 tokens). Nothing in Harmony can state that constraint, so
  nothing could have predicted it.
- **The first attempt took the unit down.** fp8 KV made FlashInfer JIT-compile at
  startup, CUDA 12.9's nvcc refused the system gcc-15, and `llm_general` crash-looped for
  about 5 minutes before a manual rollback.
- **Demand was unknowable per adapter.** The only history was vLLM's 10-second
  `Running/Waiting/KV usage` journal lines. Over 7 days they show concurrency never
  above 11 (with `--max-num-seqs 32`), queueing in ~10% of samples at 91–100% KV usage,
  and up to 112 requests waiting. Nothing says which adapter or caller those requests
  were, or how long their prompts were.

This is an optimization problem: choose compositions that minimise a defined cost under
hard memory and KV limits. At our scale (a few cards, a few base models, a handful of
adapters) an exact search is cheap and optimal. What makes any decider good or bad is
its inputs: measured costs, recent demand, and recorded outcomes. Those have to exist
before a better decider can.

The decider must also be replaceable. Today it is an exact search. Tomorrow the change
could be carried out by a Jingway woven routine, and a trained fast model ("System 1")
could propose compositions in milliseconds when a burst hits. Neither is safe unless
feasibility and cost live outside the decider, and unless every past decision and outcome
is on record to train and replay against. That is the foundation this change lays.

**Design records realised:**
- `_plans/resource-planner.md` §2 (footprint is measured) and its open questions on cost
  weights and footprint drift. **Stale in it:** §2 says a footprint MUST be measured
  weights + peak activation, but harmony-llm declares `footprint_gb` and measures nothing.
  The document also never mentions adapters, KV cache or context length, which now
  dominate whether a unit fits.
- `_plans/decision-ledger.md` §3 and §5 (outcomes joined by id; `retro.py` replay).
  **Stale in it:** placement decisions still record no outcome, and `plan()` records
  carry no footprints or device capacities, so they cannot be replayed.
- `openspec/changes/scheduler-policy-routine/design.md` §10 names this change's
  prerequisite for a future `livestack.planner.place` family: "today's host ledger
  records lack unit footprints and device capacities, so `plan()` cannot be replayed from
  them". This change supplies that state, and respects §8's rules for follow-up families.

## What Changes

1. **Measured unit cost.** After vLLM reports ready, harmony-llm parses its startup
   memory breakdown (weights + non-torch, peak activation, CUDA graphs, KV bytes, KV
   tokens, max concurrency) and reports it on `/residence` with provenance, together with
   the composition that produced it. A declared `footprint_gb` becomes a prior, used only
   until a measurement exists. A unit whose measurement failed reports `unknown`, never 0.
2. **Inference demand log.** harmony-llm's proxy path writes one bounded record per
   served request: time, unit, adapter (or base), prompt and completion tokens, end-to-end
   and queue time where known, and the outcome class. The HostBroker keeps its decayed
   per-kind demand scalar, and the log adds the per-adapter detail the scalar cannot
   hold.
3. **Replayable placement records.** Each `plan()` ledger record gains a content-addressed
   pointer to the `WorldState` snapshot it decided on, including footprints, device
   capacities and measured free memory. Given the snapshot, `plan()` can be re-run and
   must reproduce the recorded actions.
4. **The composition problem, as a pure module.** `composition.py` defines `Composition`
   (per device: base model, adapters, KV dtype, max context, batch cap),
   `feasible(state, c)` (hard limits, evaluated in code and never tunable), and
   `cost(state, c, trace, weights)`, which scores a composition by replaying recent
   demand through a small model of vLLM's admission. The weights come from a versioned
   artifact.
5. **One decider interface, one decider.** `Composer.propose(state) -> [Composition]`,
   with `ExhaustiveComposer` as its only implementation. Every proposal is checked by the
   same `feasible` and `cost`, whoever produced it.
6. **Composition decisions and outcomes on the ledger.** A composition run writes one
   ledger decision (candidates, feasibility reasons, costs, chosen, weights version,
   snapshot pointer). When a proposed composition is later applied, the first measured
   startup and the next window of demand-log outcomes are joined to it by
   `parent_decision_id`. That includes predicted-vs-measured memory.
7. **Propose, never apply.** A dry-run endpoint `GET /composition` on the host broker
   returns the proposal and a diff against the live units file. Applying it stays a
   person's action in this change. The apply-and-verify gates are specified so a later
   woven routine can carry them out.

## Capabilities

### New Capabilities
- `unit-measured-cost`: a unit's memory cost comes from what its engine reported at
  startup, with provenance. A declared figure is only a prior, and unknown is never 0.
- `inference-demand-log`: a bounded, per-request record of what each unit and adapter
  served, sufficient to replay demand.
- `unit-composition`: the composition problem (state, feasibility, cost), the decider
  interface, and the decision/outcome record every decider shares.

### Modified Capabilities
- None. No current spec covers `planner.py`, residency or the decision ledger. The
  `plan()` snapshot requirement is written under `unit-composition` rather than
  inventing a planner spec here.

## Impact

- `node-py/livestack_node/`: new `composition.py` (pure) and `composition_replay.py`
  (the admission model); `hostbroker.py` (snapshot pointer on `_emit_plan`,
  `GET /composition`); `ledger.py` (snapshot store and its bound); `facade.py`
  (`/residence` carries measured cost).
- `~/harmony-llm/server.py` (not in this repo): startup-line parser, demand-log writer.
  Its source of truth needs settling; see design §9.
- `HARMONY.md`: operator reference for measured cost, the demand log, and reading a
  composition proposal.
- No change to request routing, `/v1/classifier`, callers, or the request language.

## Non-goals

- **Applying compositions automatically**, or restarting units on a schedule. Every apply
  is a person's action. The weave that will carry it out is a follow-up change.
- **A learned decider.** The interface and the training record are in scope; the model
  is not.
- **Compiling composition as a Jingway `PolicyFamily`.** Whether it should be one, or a
  sibling that shares only the record format and the replay tooling, is design §8's open
  question for a person.
- **Hot-loading adapters at runtime** (`/v1/load_lora_adapter`). It is compatible with
  this design and deferred.
- **Cross-host placement or eviction.** `harmony-gaps-2026-09.md` keeps that off Harmony's
  list. This change composes units per host, and the host broker stays the sole residency
  authority.
