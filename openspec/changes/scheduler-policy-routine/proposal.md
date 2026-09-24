## Why

`fleet_scheduler.schedule()` picks where each job runs: which running target it is
admitted to, or which elastic pool to provision, up to RunPod as the last resort. The
choice is a weighted score (`_score`: `w_budget·cost + w_speed·eta + w_distance·distance
+ w_utilization·utilization − w_resource·local_bonus`). Every number in that score was
chosen by an engineer and has never been checked against outcomes. `_plans/fleet-scheduler.md`
§5 describes weights that tune themselves (`resolve_weights`), but production never calls
it: `hostd.py:1133` builds `SchedulerPolicy()` with default weights.

The decision is about to get more consequential. Once pools are declared
(`openspec/changes/fleet-provisioning-activation/`), the same score decides between a free
local GPU, cheap cloud, and paid serverless, and a wrong weight costs money or makes
users wait. That is a tuning problem, and it should be solved with evidence.

Jingway now has a compiled-policy tier for this
(`jingway/openspec/changes/compiled-policy-routines/`, read its `design.md` first). The
decision's structure is compiled Rust with the Python code kept as reference. Its weights
are a versioned artifact. Every decision is recorded with the propensities off-policy
evaluation needs, and the Jingway improver tunes the weights by replaying recorded
decisions through the same compiled code. This change makes the scheduler's target choice
that tier's first real consumer.

It is also a deliberate dogfood. The choice is fast, runs often and almost never needs a
model: the opposite shape from every routine the improver has handled so far.

**Design records realised:**
- `_plans/fleet-scheduler.md` §5 (weights that vary with evidence). **Stale in it:** §5
  and §7 present `resolve_weights` as the live mechanism, but nothing in production calls
  it. This change adds a note saying so and points at the policy artifact as the mechanism
  that actually ships.
- `_plans/decision-ledger.md` §3 (outcomes joined by id). **Stale in it:** the admit
  record's `outcome` means only that a lease was obtained. No job outcome (how long the
  work held the slot, whether it finished or expired) is joined to the decision. This
  change adds one.

## What Changes

1. **Extract the choice.** Refactor `schedule()`'s per-job target choice into a pure
   `choose_target_reference(params, ctx, candidates) -> rows`, a Python mirror of the
   Jingway family API. The move must preserve behaviour, which a golden corpus recorded
   before the refactor proves. The outer loop stays in Python: EDF job order, quotas, the
   free-capacity bookkeeping, and deprovision.
2. **Native family.** New Rust crate `native/policy/` (package `livestack-policy`)
   implementing family `livestack.fleet.choose_target` v1 on `jingway-policy`. It builds:
   - a PyO3 module `livestack_policy` (abi3);
   - the replay CLI binary `livestack-policy`.
   
   Python imports the module optionally, falls back to the reference, and reports the
   fallback.
3. **The first policy artifact** `livestack.fleet.choose_target`, holding exactly
   today's defaults, with exploration disabled. Hosts read it from a local file with the
   compiled defaults as fallback.
4. **Recording.** Every `/fleet/admit` choice is recorded in exactly the
   `jingway.policy_decision/v1` format into a separate stream:
   - **Writer:** the native non-blocking `Recorder` (Jingway option (b)), with its own
     bound sized in days.
   - **Existing ledger:** the admit record gains only a small pointer
     (`decision_id`, `artifact_version`, `chosen`, `explored`).
   - **Outcomes:** appended to the same stream when the lease ends (released vs expired,
     held seconds, and the caller-reported status and wall time when given).
   - **Why a separate stream:** the baseline found the fleet ledger retains only 13.6 h,
     and a synchronous append costs 5–7× the decision.
5. **Improver host.** A small scheduled TS job in `fleetd/` (`policy-improver`) with its
   own PGLite activation ledger. Each run:
   - reads the ledger files and replays them through the `livestack-policy` CLI;
   - runs Jingway's `tune_policy`;
   - writes proposals a person reads.
   
   Activation publishes the artifact to the fleet broker through a new authenticated
   `PUT /fleet/policy/{policy_id}`.
6. **Shadow.** The broker can evaluate up to two candidate artifacts beside the active
   one and record their choices without acting on them.
7. **Recursion rules for Harmony,** written down now even though this first family barely
   touches them (design.md §8).

## Capabilities

### New Capabilities

- `fleet-scheduler-policy`: the compiled target-choice policy — its reference/native
  equivalence, its artifact lifecycle on the broker, its decision and outcome records,
  exploration limits, shadowing, and the recursion rules for Harmony.

### Modified Capabilities

None. `fleet-provisioning-operations` and `fleet-supervision-loop` are unchanged. The
scheduler still decides; this change only makes its parameters data, its choice recorded,
and its implementation swappable.

## Impact

- **Python** (`node-py/livestack_node`):
  - `fleet_scheduler.py` is refactored behaviour-preserving;
  - new `policy_runtime.py` (artifact loading, native/reference selection, shadow);
  - `fleet_admit.py`, `fleet_ops_api.py`, `hostd.py` and `hostbroker.py` gain the
    recording fields and the policy routes;
  - `ledger.py` and `decision.schema.json` gain the small `policy` pointer on admit records.
- **Rust**: new standalone Cargo workspace `native/policy/`, depending on `jingway-policy`
  pinned to a git revision.
- **TS** (`fleetd/`): new `src/policy/` and a `policy-improver` entry point.
- **Deployment:**
  - the `livestack_policy` `.so` ships inside the release directory the broker's
    `PYTHONPATH` drop-in names;
  - a systemd timer runs the improver;
  - storage bounds for the new files are recorded (design.md §9).
- **Behaviour:** none on day one. Artifact = today's defaults and exploration is off. The
  first behaviour change is a person enabling exploration (task 7.2).

## Non-goals

- **Not compiling Harmony's other decisions yet:**
  - `server.py` resident-first unit selection;
  - `planner.plan()` placement;
  - `fleet_rank.rank()`.
  
  They are listed as follow-up families (design.md §10). The recursion rules in §8 are
  written for them.
- Not turning on pools, provider credentials, or any spending. That stays with
  `fleet-provisioning-activation`.
- Not wiring `resolve_weights` (time-of-day, pressure). If evidence shows time of day
  matters, it becomes a context feature in family v2.
- Not training a cost or latency predictor. Outcomes are recorded so one can be trained
  later.
- Not auto-promotion. The artifact ships with no envelope, so every promotion needs a
  person until someone chooses an envelope (Jingway design §14 Q2).
