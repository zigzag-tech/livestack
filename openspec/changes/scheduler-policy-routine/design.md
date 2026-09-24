# Design — scheduler-policy-routine

**Read first, in this order:**
1. `jingway/openspec/changes/compiled-policy-routines/design.md`: the framework this
   change consumes. Its glossary (§0) is used here without re-definition.
2. `node-py/livestack_node/fleet_scheduler.py`: all of it, especially
   `_feasible_candidates`, `_score`, `_distance_n`, `_utilization_n`, `_norm`,
   `schedule()`.
3. `node-py/livestack_node/fleet_admit.py` `admit()`,
   `node-py/livestack_node/fleet_ops_api.py` `build_plan()` and `policy_digest()`, and
   `hostd.py` `/fleet/admit` (≈ line 547).
4. `node-py/livestack_node/ledger.py` and `decision.schema.json`.
5. `_plans/decision-ledger.md` §1–§3.

Baseline numbers: `receipts/baseline-benchmark.md` (task 0.1). If a number here conflicts
with it, the receipt wins; correct this file in the same commit.

---

## 1. Who owns what, and how ids cross (config rule)

| durable state | owner process | where | written by |
|---|---|---|---|
| Active / previous / shadow artifacts | fleet broker (`livestack-fleetd.service`, `hostd` on :8801) | `$LIVESTACK_POLICY_DIR` (default `~/.local/share/livestack/policy/`): `<policy_id>.active.json`, `.previous.json`, `.shadow.json` | only the `PUT /fleet/policy/{policy_id}` route (§6) |
| Policy decision + outcome records | fleet broker | a SEPARATE bounded stream `$LIVESTACK_POLICY_DIR/records/livestack.fleet.choose_target.jsonl*`, written by the native `Recorder` (Jingway option (b), J§6.4) in exactly the J§6.2/§6.3 formats | `PolicyRuntime` (after each committed decision) and `hostbroker` at lease release/expiry |
| Admit audit record (existing) | fleet broker | the existing decision ledger (`fleet-decisions.jsonl*`) | `hostbroker.emit_admit`, gaining ONLY a small pointer `policy: {decision_id, artifact_version, chosen, explored}` |

**Why a separate stream (from the baseline receipt).** The fleet ledger currently retains
**13.6 h** (64 MiB × 4, ~7.5 KB per admit record, bursts of ~1.8 admits/s filled a 64 MiB
file in ~1.4 h), and a synchronous `JsonlLedger.append` costs 305–446 µs on the tower.
A 7-day improver window cannot live there, and the policy block would make both problems
worse. The native Recorder is non-blocking (J§6.4) and gets its own bound sized in DAYS
(§9). The ledger keeps its operator-audit role unchanged plus a pointer for the join.

**When the native module is absent** (mode `0`, or `auto` without the module), no policy
records are written — the Recorder is native — and `/fleet` reports degraded
`policy_records_unavailable`. That is loud, not silent, and compare mode (the only
rollout mode before cutover) always has the module.
| Activation ledger + proposals + artifact store | `policy-improver` (TS, fleetd package) | PGLite at `$LIVESTACK_POLICY_IMPROVER_DB` (default `~/.local/share/livestack/policy-improver/pg`); artifact store `…/policy-improver/artifacts/<version>.json` | the improver only |

**Authority.** The activation ledger says which artifact *should* be active. The
broker's `.active.json` is a projection of it. On every run the improver `GET`s the
broker's active version and compares. On a mismatch it reports `policy_projection_drift`
and does nothing else: it never overwrites without an activation transition. The broker
never reads the improver's DB.

**Ids that cross boundaries.**

| id | minted by | carried |
|---|---|---|
| `decision_id` | `hostd` (`ledger.new_decision_id()`, monotonic ULID), **before** deciding, because it seeds exploration | ledger record → `/fleet/admit` response field `decision_id` (new) → stored on the hosted lease entry → outcome records at release/expiry |
| `artifact_version` | the `livestack-policy` binary / native module (Rust computes it; Python and TS never hash) | improver → `PUT` body → broker file → every decision record → improver replay |
| `lease_id` | `hostbroker.hosted_checkout` (unchanged) | response → caller's heartbeat/release → joins outcome to decision through the lease entry |
| `job_id` | unchanged | unchanged |

---

## 2. The family `livestack.fleet.choose_target`, version 1

This is a faithful port of the per-job choice inside `schedule()`. Anything that depends
on state mutated *across* jobs within one `schedule()` call stays in Python and arrives as
a per-candidate boolean feature:
- free capacity (`W.free`);
- pool headroom (`W.provisioned`);
- usage and quotas.

### 2.1 Context (`F::Context`)

```jsonc
{ "now": 1788600000.0,
  "job": { "id": "asr-1788600000000", "sla": "interactive" | "normal" | "batch",
           "created_at": 1788600000.0, "deadline": null | 1788600030.0,
           "est_duration_s": 60.0, "locality_host": null | "zz-tower0" } }
```

### 2.2 Candidate features (`F::Features`), one per target in `state.targets` order

```jsonc
{ "host_id": "xc-tower-ubuntu",
  "tier": "LOCAL" | "SPOT" | "ONDEMAND" | "LAST_RESORT",
  "running": true, "elastic": false,
  "selector_match": true,        // _selector_matches(t, job.selector)
  "fits_now": true,              // running only: _fits(job.need, W.free[t.id]) at this job's turn
  "headroom_ok": false,          // elastic only: W.headroom(t) > 0 at this job's turn
  "fits_instance": false,        // elastic only: _fits(job.need, t.capacity)
  "provision_latency_s": 0.0, "cost_per_hour": 0.0, "cost_per_job": 0.0,
  "distance_ms": null | 16.2, "utilization": null | 0.4 }
```

Candidate `id` = `Target.id`. Booleans that do not apply (e.g. `fits_now` for a pool)
are `false` and never read.

### 2.3 Params (flat names; defaults = today's code exactly)

| name | default | hard bounds | search |
|---|---|---|---|
| `w_resource` | 1.0 | [0, 10] | Linear |
| `w_budget` | 1.0 | [0, 10] | Linear |
| `w_speed` | 1.0 | [0, 10] | Linear |
| `w_distance` | 2.0 | [0, 10] | Linear |
| `w_utilization` | 1.0 | [0, 10] | Linear |
| `distance_by_sla.interactive` | 1.0 | [0, 5] | Linear |
| `distance_by_sla.normal` | 0.5 | [0, 5] | Linear |
| `distance_by_sla.batch` | 0.1 | [0, 5] | Linear |
| `local_bonus` | 1.0 | [0, 5] | Linear |
| `locality_bonus` | 0.5 | [0, 5] | Linear |
| `sla_slack_s.interactive` | 30 | [1, 604800] | **Fixed** |
| `sla_slack_s.normal` | 1800 | [1, 604800] | **Fixed** |
| `sla_slack_s.batch` | 43200 | [1, 604800] | **Fixed** |

The slacks are params so that a person can change them through an artifact. They are
`Fixed` because they define what an SLA *means*, not how to trade one objective against
another, and the improver must not optimise them away. `MAX_EPSILON = 0.10`.

`min_uptime_s`, quotas and `fair_share_penalty_s` are **not** in this family. They belong
to the outer loop and deprovision, and stay in `SchedulerPolicy`.

### 2.4 `evaluate` — exact algorithm

For each candidate in input order, the first failing check sets `eligible = false`,
`score = None`, `explorable = false` and the reason code:

1. `!selector_match` → `filtered:selector`
2. `deadline = job.deadline ?? job.created_at + sla_slack_s[job.sla]`;
   `eta = running ? 0.0 : provision_latency_s`;
   `now + eta > deadline + 1e-9` → `filtered:deadline`
3. if `running`: `!fits_now` → `filtered:no_room` (provision = false)
   else if `elastic`: `!headroom_ok` → `filtered:pool_at_cap`; `!fits_instance` →
   `filtered:instance_too_small` (provision = true)
   else → `filtered:cold_not_elastic`
4. `est_cost = cost_per_job + cost_per_hour * (max(0, est_duration_s) / 3600)`

Let `C` = the candidates that passed 1–3. **Last-resort guard:** if any member of `C` has
tier < `LAST_RESORT`, every `LAST_RESORT` member becomes ineligible with
`filtered:last_resort_guard`. The rest of `C` is `E`, the eligible set.

For each `c ∈ E`, all normalisations are over `E` only, matching `_score(c, cands, …)`
where `cands` is the post-guard list:

```
norm(v, xs)  = (hi - lo < 1e-9) ? 0.0 : (v - lo) / (hi - lo),  lo = min(xs), hi = max(xs)
cost_n       = norm(c.est_cost, [e.est_cost for e in E])
eta_n        = norm(c.eta,      [e.eta      for e in E])
known_d      = [e.distance_ms for e in E if not null]
dist_n       = known_d empty ? 0.0 : norm(c.distance_ms ?? max(known_d), known_d + [that value])
known_u      = [e.utilization for e in E if not null]
median_u     = sorted(known_u)[len(known_u) / 2]      // integer division: UPPER median, as Python
util_n       = known_u empty ? 0.0 : norm(c.utilization ?? median_u, known_u + [that value])
d_scale      = distance_by_sla[job.sla]
local        = (tier == LOCAL ? local_bonus : 0.0) + (locality_host != null && locality_host == host_id ? locality_bonus : 0.0)
score        = ((((w_budget*cost_n + w_speed*eta_n) + (w_distance*d_scale)*dist_n) + w_utilization*util_n) - w_resource*local)
```

**Keep that exact association order.** Python evaluates
`weights.budget * cost_n + weights.speed * eta_n + weights.distance * distance_scale * _distance_n(...) + weights.utilization * _utilization_n(...) - weights.resource * cand.local_bonus`
left to right, and `w_distance * d_scale` is computed first because Python's `*` is
left-associative. Different association gives different floating-point results, and the
differential test (§4) will catch it.

- The reason for an eligible row is
  `scored:{score:.6f} cost_n={..} eta_n={..} dist_n={..} util_n={..} local={..}`.
  Differential tests compare only the code before the first space.
- **explorable** = eligible AND `running` AND tier ≠ `LAST_RESORT`. The consequence:
  exploration can move a job between machines that are already running, but it can
  **never** spend money by provisioning, and it can never pick RunPod.
- **escalate:** v1 never escalates. The field exists for v2, where a family could escalate
  on, say, every candidate's distance being unmeasured.

The greedy choice and tie-break follow the Jingway `decide()` contract: lowest score,
earliest input position. That matches `min(cands, key=…)` in `schedule()`.

---

## 3. Python refactor (behaviour-preserving)

New module `node-py/livestack_node/policy_runtime.py`:

```python
POLICY_ID = "livestack.fleet.choose_target"
FAMILY = (POLICY_ID, 1)
DEFAULT_PARAMS: Dict[str, float]            # §2.3 defaults
def params_from_policy(policy: SchedulerPolicy) -> Dict[str, float]   # for tests/back-compat
def choose_target_reference(params, ctx: dict, candidates: List[dict]) -> List[dict]
    # pure Python mirror of §2.4; returns rows [{id, eligible, score, explorable, reason}]
class PolicyRuntime:
    # one per broker process
    def __init__(self, policy_dir: str, mode: str, self_principals: FrozenSet[str], log)
    def decide(self, ctx: dict, candidates: List[dict], decision_id: str) -> dict
        # returns the Jingway Decision shape (dict): rows, greedy, chosen, explored,
        # explore_set, propensities, exploration, artifact_version, family, shadow: [...]
    def status(self) -> dict                  # for GET /fleet and health (§7)
    def reload_if_changed(self) -> None       # mtime poll, at most every 5 s, called from decide
```

`schedule()` changes minimally:

```python
def schedule(state, policy=None, *, runtime: Optional[PolicyRuntime] = None,
             decision_ids: Optional[Mapping[str, str]] = None) -> FleetPlan
```

- `runtime=None` → a module-level `_REFERENCE_RUNTIME`: reference implementation, default
  params, exploration off. Existing callers and tests are therefore unchanged.
- Per job, build `ctx` + `candidates` (§2.1–2.2) from `W` at that job's turn, call
  `runtime.decide(...)`, then act on `decision["chosen"]`. Find the target; if its row came
  from a pool, emit `Provision`, otherwise `Admit`. Do the same capacity bookkeeping as
  today. `None` → `Queue(job.id, "no feasible target meets the deadline now")`. Keep that
  sentence exactly; callers match on it.
- Attach the decision to the plan without changing existing action types:
  `FleetPlan.decisions: Mapping[job_id, dict]` (new field, default empty).
- `_feasible_candidates` and `_score` stay and are still used by the reference. The
  reference is a thin wrapper that calls them, so it cannot drift from the code it
  replaces. Where the wrapper needs a value `_feasible_candidates` does not expose, such
  as the guard reason, extend it with a keyword-only flag rather than duplicating it.

**How the refactor is proved behaviour-preserving (task 2.1).** Before touching
`schedule()`:
1. Commit a generator `node-py/tests/policy_golden/generate.py` that builds 5 000 random
   `FleetState`s. Use a fixed seed. Draw 0–12 targets across all tiers, running and
   elastic, with measured and unmeasured distance and utilization, and 1–6 jobs across all
   SLAs with and without explicit deadlines and locality.
2. Record `schedule(state).summary()` plus the action tuples as `golden.jsonl`.
3. After the refactor, the test `test_policy_golden.py` requires byte-identical output.

---

## 4. Native crate `native/policy/`

```
native/policy/Cargo.toml          [workspace] (standalone — NOT a member of the repo-root workspace)
  members = ["family", "py", "cli"]
native/policy/family/             package livestack-policy-family (rlib): ChooseTarget: PolicyFamily
native/policy/py/                 package livestack-policy-py (cdylib, lib name "livestack_policy"):
                                  #[pymodule] livestack_policy → jingway_policy_py::add_policy_module(registry)
native/policy/cli/                package livestack-policy-cli, bin "livestack-policy": jingway_policy::cli::run_cli(&Registry)
native/policy/pyproject.toml      maturin, manifest-path py/Cargo.toml, abi3-py38
native/policy/.cargo/config.toml.example   local [patch] pointing jingway-policy at ~/jingway/core/crates/policy
```

- **Dependency:** `jingway-policy = { git = "ssh://git@github.com/zigzag-tech/jingway.git", rev = "<merged commit>" }`,
  and the same for `jingway-policy-py`. Pin `rev`, never a branch. For local development,
  copy the `.example` to `.cargo/config.toml`, which is gitignored.
- **Differential test** (`native/policy/family/tests/differential.rs` +
  `node-py/tests/test_policy_differential.py`):
  1. Python generates 10 000 cases with a fixed seed: random params inside the hard
     bounds, and contexts and candidates as in the golden generator.
  2. It writes them as JSONL and runs `choose_target_reference` on each.
  3. It runs the native `livestack_policy.decide` on the same case.
  4. It asserts identical eligibility and reason codes, the same greedy choice, and
     scores within 1e-12.
  
  Any mismatch is written to `node-py/tests/policy_golden/mismatch-<n>.json` and fails the
  test.
- **Invariant fixtures** for the Jingway ladder's cornerstones rung, in
  `native/policy/family/tests/fixtures/livestack.fleet.choose_target/invariants/`. Each
  must hold for ANY params:
  1. LAST_RESORT is never eligible while a cheaper tier is feasible.
  2. A candidate with `selector_match=false` is never chosen.
  3. With exploration on, a `running=false` candidate is never chosen unless it is greedy.
  4. A deadline-infeasible candidate is never chosen.

---

## 5. Exploration on the broker

- It applies **only on `/fleet/admit`**, which commits one choice once.
  `/fleet/plan` runs greedy with exploration forced off. It is called every tick for the
  same queued jobs, and a fresh decision id per tick would re-draw every tick and flap a
  job between targets. The plan response reports `exploration: "off_on_plan_path"`.
- The first artifact ships `exploration.enabled = false`. Turning it on is a person's
  decision (task 7.2), because it deliberately sends a small share of jobs to a target the
  score ranked second. Recommended first setting: `epsilon = 0.05`, `margin = 0.25`.
  Only running targets within 0.25 score units of the best are ever candidates.
- Exploration never provisions and never selects `LAST_RESORT`, by construction (§2.4
  explorable).

---

## 6. Artifact lifecycle on the broker

**`PUT /fleet/policy/{policy_id}?role=active|shadow`**
- **Body:** the artifact JSON. For `role=shadow`, a JSON array of at most 2 artifacts.
- **Auth:** a fleet principal carrying the new capability `policy_admin`. Add it to the
  token file schema that `fleet_auth.principals_from_env` reads. If fleet auth is OFF,
  the route answers **403** `policy publishing requires fleet auth`. It never accepts an
  unauthenticated publish, because this route changes routing for everyone.
- **Validation:** `livestack_policy.load_artifact(body)` checks the family, params,
  bounds, exploration and the recomputed version. If the native module is unavailable the
  route answers **503** `cannot validate artifact without livestack_policy`; it never
  accepts an artifact it could not validate. Violations answer **422** with the full list.
- **Write:** atomic. Write a temp file, fsync, rename. For `active`, first copy the
  current `.active.json` to `.previous.json`. The file lives in `$LIVESTACK_POLICY_DIR`.
- **Response:** `{policy_id, role, version, previous_version}`.

**`GET /fleet/policy/{policy_id}`** returns `{active: {version, provenance, loaded_at},
previous: {version}, shadow: [{version}], source: "file" | "defaults", native: bool,
mode, mismatches, last_load_error}`.

**Loading.** `PolicyRuntime.reload_if_changed()` polls the mtimes at most every 5 s,
from inside `decide`. The poll is one `os.stat` per 5 s, and it re-reads a file only when
its mtime changed.
- A file that fails validation leaves the previously loaded artifact in force, sets
  `last_load_error`, and logs one line naming every violation.
- If no active file exists: default params, `source: "defaults"`, and health reports
  `policy_artifact_missing`, which counts as degraded.

**Local revert.** If a guardrail reported by the improver trips (§8.3),
`POST /fleet/policy/{policy_id}/revert` (auth `policy_admin`) swaps `.previous.json`
back into `.active.json` without any model or improver involvement. The improver calls
this route after recording the activation transition. A person can call it by hand.

**`policy_digest()`** (`fleet_ops_api.py`) adds the active `artifact_version`, so
`plan_version` changes when the policy changes, which is exactly what the digest exists
to detect.

---

## 7. Native/reference mode and health

`LIVESTACK_POLICY_NATIVE`:

| value | behaviour |
|---|---|
| `0` | Reference only. |
| `auto` (default) | Native if importable, else reference plus degraded `policy_native_unavailable`. |
| `compare` | Run both on every decision, act on the **reference**, count mismatches. Log each mismatch once with its decision id, and write the case to `$LIVESTACK_POLICY_DIR/mismatches/` (bounded: 100 files, oldest deleted). |

Rollout (task 6.3): deploy with `compare` for ≥ 7 days or ≥ 5 000 decisions, whichever is
later. Switch to `auto` only with `mismatches == 0`.

`GET /fleet` gains `policy: PolicyRuntime.status()`. The broker is reported
**unhealthy**, via the existing subsystem-failure mechanism if there is one (check
`hostd.py`), otherwise a `degraded: [..]` list on `/fleet`, when:
- `mismatches > 0`;
- `last_load_error` is set;
- `source == "defaults"` while a policy dir is configured;
- the policy Recorder reports `dropped > 0` or a `last_error`, or records are unavailable (§1).

---

## 8. Recursion: Harmony routes Jingway, and Jingway tunes Harmony

State the fact plainly in `docs`, and here:

- **This first family barely touches the recursion.** `/fleet/admit` places *jobs*:
  workloads and ASR/TTS callers. Jingway's own LLM calls go to Harmony's
  `server.py` → `hostd /admit` → `planner.plan()`, a different decision this change does
  not compile. The improver loop is also inference-free: `tune_policy` is deterministic
  search plus replay plus estimators, and the Jingway synthesizer (the only improver part
  that calls a model) is not used for `policy_params`. No inference means no deadlock and
  no lock-out, whatever the policy does.
- **Rules that apply now:**
  1. `decide` never infers.
  2. `self_traffic` is true when the request's authenticated principal is in
     `LIVESTACK_POLICY_SELF_PRINCIPALS` (comma-separated principal names). Set it to the
     principals fleetd and the improver use.
  3. Revert is a file swap (§6).
  4. With no artifact, the broker decides with defaults.
- **Rules that follow-up families MUST add** (`planner.place`, `harmony.llm.select_unit`;
  §10):
  1. The unit(s) serving Jingway's improver and repair traffic are named in
     `/etc/harmony/llm-units.json` with `residency: HARD_PIN`. The planner family must mark
     every candidate that would evict a HARD_PIN unit as ineligible
     (`filtered:hard_pin`), in code, not as a tunable.
  2. Escalation from a Harmony-path family is acted on only after the request is served,
     and only against a resident named unit. Otherwise it is recorded as
     `escalation_skipped:not_resident`.
  3. Outcomes of requests whose principal is Jingway's own are `self_traffic` and excluded
     from objectives by default.

---

## 9. Storage bounds (benchday rule 10 applies to livestack; record them)

| store | bound | enforcer |
|---|---|---|
| policy record stream `$LIVESTACK_POLICY_DIR/records/` | `LIVESTACK_POLICY_RECORDS_MAX_MB` × `LIVESTACK_POLICY_RECORDS_FILES`, default **128 MiB × 16 = 2 GiB**; age window `LIVESTACK_POLICY_RECORDS_AGE_DAYS` default **unset (disabled)** | native `Recorder` rotation (J§6.4) |
| decision ledger (existing) | unchanged, 64 MiB × 4 on the fleet broker; the admit record grows only by the ~150 B pointer | `JsonlLedger` rotation |
| `$LIVESTACK_POLICY_DIR` artifacts | active + previous + shadow = 3 files; `mismatches/` ≤ 100 files | the route writes by rename; `PolicyRuntime` prunes `mismatches/` |
| improver PGLite | proposals: keep newest 365 per policy; activations: all (≤ a few per week) | improver prunes at end of each run |
| improver scratch | replay output dir deleted at end of each run | improver |

**Sizing the record stream is task 3.3's job, with arithmetic, not a guess.** Measure the
median policy record size (context + ~14 candidates' features + rows) and the observed
AVERAGE admit rate (the receipt's current-file rate is ~5.5 MB/h of 7.5 KB records ≈ 730
admits/h; bursts reach ~1.8/s). Required: ≥ **21 days** (three weekly windows) at the
average rate. If 2 GiB does not reach 21 days, raise the bound up to 8 GiB and write the
arithmetic into `_plans/decision-ledger.md`; if 8 GiB is not enough, STOP and ask — the
answer is then a smaller record (e.g. feature arrays instead of objects), not sampling,
because exposures are never sampled (Jingway `exposure-log`).

**Also worth a person's look, not fixed here:** ~1.8 admits/s bursts on the fleet broker
are far above what the known callers suggest. Before tuning on this traffic, find who is
admitting that often (group the ledger's admit records by `request.owner`/`principal`);
a retry loop would dominate every estimate. Task 0.1 does this.

## 10. Follow-up families (NOT this change; one change each)

| family | code today | what would be tuned | recursion exposure |
|---|---|---|---|
| `livestack.planner.place` | `planner._best_placement` (reload_cost, `_contention_cost`, 50 per busy victim, slack tie-break) | the victim/contention constants | **high**: evictions affect the model Jingway itself uses (§8 rules). **Prerequisite (baseline receipt §5):** today's host ledger records lack unit footprints and device capacities, so `plan()` cannot be replayed from them; the family's context must carry them |
| `harmony.llm.select_unit` | `server.py _selection_rank` (default unit first, then name) | preference among satisfying units; needs load/latency features first | high: on the path of every LLM call |
| `livestack.fleet.rank` | `fleet_rank.rank()` lexicographic sort | would need converting to a score first; the lexicographic order is deliberate | low |
| `livestack.fleet.choose_target` v2 | this family | time-of-day and pressure as context features (replacing `resolve_weights`), a learned ETA/cost predictor as a feature | low |

---

## 11. Open questions for a person (ask; do not decide)

1. **Objective.** Proposed: primary `job_wall_s` (min), with floors on `lease_expired`
   rate (≤ incumbent + 0.01) and `caller_ok` (≥ incumbent − 0.01). It only works if
   callers report `wall_s` at release (task 4.2 makes `lease_helper.py` do so). Is that
   the right thing to optimise, or is it time-to-start once pools exist?
2. **When to enable exploration** (task 7.2), and at what ε.
