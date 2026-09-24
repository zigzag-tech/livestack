# Tasks — scheduler-policy-routine

Read `proposal.md`, `design.md`, and the Jingway change
`jingway/openspec/changes/compiled-policy-routines/` (proposal + design) before task 1.
Section references (§n) point at this change's `design.md` unless prefixed "J§", which
means the Jingway design.

**Where to run things.** Build Rust and run test suites OFF `xc-tower-ubuntu` (it carries
Harmony and vLLM; the fleet broker also runs there). Use `xc-mac-studio` (`ssh 100.64.0.2`)
for development and tests. Build the Linux x86_64 production artifacts (task 6.1) on a
Linux host that is not the tower: `xc-win-1` WSL has cargo at `~/.cargo/bin`, 16 cores.
Work in a worktree (`git worktree add -b agent/scheduler-policy-routine ~/worktrees/livestack/scheduler-policy-routine main`)
and commit per task.

**Production actions need a person's go-ahead.** Tasks marked **[ASK]** touch the live
fleet broker (`livestack-fleetd.service` on xc-tower-ubuntu) or change routing. Before
each one, say exactly what you will run and wait for a yes.

**Each task names its tests and its ledger obligation** (livestack config rule). "Ledger:
none" means the task adds no decision or outcome emission.

**Dependencies.** Tasks 1–3 can start at once. Task 4 needs Jingway groups 1–4 merged.
Task 5 needs Jingway groups 5–6 merged.

## 0. Baseline

- [x] 0.1 The baseline receipt is already in `receipts/baseline-benchmark.md` (2026-09-24). It measured the ledger's retention (fleet: 13.6 h) but NOT who is admitting. From the live fleet broker (read-only), group the `"decision": "admit"` records in `~/.cache/livestack/fleet-decisions.jsonl*` on xc-tower-ubuntu by `request.owner` and `request.principal`, per hour. Report the average and peak admit rate, the top 5 callers, and whether the ~1.8 admits/s bursts look like a retry loop (the same owner/kind repeating within seconds with no lease released). Append this to the receipt as section "admit traffic". If it is a retry loop, STOP and tell the operator before continuing: it would dominate every estimate this change produces.
  Tests: none. Ledger: none. Verify: the receipt has an "admit traffic" section with the rates and top callers.

## 1. Golden corpus BEFORE any refactor

- [x] 1.1 Write `node-py/tests/policy_golden/generate.py` (seed 20260924) producing 5 000 random `FleetState`s as described in §3, and a recorder that writes `golden.jsonl` (for each state: the state as JSON, `schedule(state).summary()`, and the action tuples as `[type, job_id, target_id|None, reason]`). Commit the generator AND `golden.jsonl`. If it exceeds 5 MB, gzip it.
  Tests: `node-py/tests/test_policy_golden.py`, which regenerates and compares. It must pass on the unmodified code. Ledger: none.
  Verify: `cd node-py && python -m pytest tests/test_policy_golden.py -q`.

## 2. Python refactor

- [x] 2.1 Create `node-py/livestack_node/policy_runtime.py` with `POLICY_ID`, `FAMILY`, `DEFAULT_PARAMS` (the §2.3 table exactly), `choose_target_reference`, and `build_ctx_and_candidates(job, fleet_W, targets, policy)`, which produces the §2.1/§2.2 JSON shapes. Modify `schedule()` per §3 (the `runtime`/`decision_ids` keyword args, and `FleetPlan.decisions`). Also add a private reference-only runtime used when `runtime=None`. Do not change `Queue`/`Admit`/`Provision` reason strings.
  Tests: `test_policy_golden.py` (must still pass byte-identically), and all existing `test_fleet_scheduler.py`, `test_fleet_admit*.py`, `test_fleet_dispatch.py` tests. Add `test_policy_reference.py`: one test per reason code in §2.4, the upper-median utilization case, unmeasured distance scoring as the max known, the tie going to the earliest input position, and the LAST_RESORT guard. Ledger: none.
  Verify: `cd node-py && python -m pytest tests/test_policy_golden.py tests/test_policy_reference.py tests/test_fleet_scheduler.py tests/test_fleet_admit.py tests/test_fleet_admit_regions.py tests/test_fleet_dispatch.py -q`.
- [ ] 2.2 Implement `PolicyRuntime` (§3, §6 loading, §7 modes) in pure Python, calling the native module when present. Load files from `$LIVESTACK_POLICY_DIR`. Poll mtimes at most every 5 s. A bad file keeps the previous artifact. No file means defaults plus `policy_artifact_missing`. Handle modes `0`/`auto`/`compare`. `compare` writes mismatch files, bounded to 100 with the oldest deleted. Implement shadow evaluation (≤ 2, greedy, recorded only). Own the native `Recorder` (J§6.4) for the policy record stream (§1, §9 env vars) and write each committed decision's record after `decide` returns. `status()` includes the Recorder's `stats()`. The Recorder is INJECTABLE (`PolicyRuntime(..., recorder=None)`) with the interface of the native one: `record(dict) -> bool` (False = dropped), `stats() -> dict`, `close()`. Groups 2–3 come before the native crate exists (group 4), so their tests use a `FakeRecorder` in `node-py/tests/policy_fakes.py`. Production constructs the native `livestack_policy.Recorder` when the module is importable; otherwise it uses none and reports `policy_records_unavailable`. Implement exploration by delegating to the native `decide`. With the native module absent, exploration is OFF even if the artifact enables it, and the status says `exploration_disabled:native_unavailable`: the draws are defined only in Rust (J§5.1), and a Python re-implementation would be a second source of truth.
  Tests: `test_policy_runtime.py` covering: no file; a corrupt file after a good one; the reload interval (inject a clock); compare-mode mismatch (inject a fake native that disagrees); shadow never changing the choice; the 100-file mismatch bound. Ledger: none (the emission is task 3).
  Verify: `cd node-py && python -m pytest tests/test_policy_runtime.py -q`.

## 3. Recording and outcomes

- [ ] 3.1 Extend `decision.schema.json` and `ledger.py` with ONLY the optional admit pointer `policy: {decision_id, artifact_version, chosen, explored}`. Decision and outcome records do NOT go into this ledger (§1); do not add record kinds here.
  Tests: `test_ledger.py`: a schema-valid admit record with and without the pointer. Ledger: **adds the pointer to `admit`**.
  Verify: `cd node-py && python -m pytest tests/test_ledger.py tests/test_ledger_attribution.py -q`.
- [ ] 3.2 In `/fleet/admit` (`hostd.py`) and `fleet_admit.admit()`:
  1. Mint `decision_id = ledger.new_decision_id()` before `schedule()`.
  2. Pass `runtime=broker.policy_runtime` and `decision_ids={job.id: decision_id}`.
  3. When `chosen` is not `None`, have `PolicyRuntime` write `plan.decisions[job.id]` as a J§6.2 record (refusals are skipped and counted in `skipped_no_choice`, §1) to the policy stream, with `self_traffic` computed from `LIVESTACK_POLICY_SELF_PRINCIPALS` and the authenticated principal. Put the pointer on the admit ledger record.
  4. Return `decision_id` in the response.
  5. Store `decision_id` on the hosted lease entry (`hosted_checkout(..., decision_id=...)`).
  
  `/fleet/plan` (`fleet_ops_api.build_plan`) passes the runtime with exploration forced off, returns `artifact_version` and `exploration: "off_on_plan_path"`, and includes `artifact_version` in `policy_digest()`.
  Tests: `test_fleet_admit.py` / `test_hostd_admit_auth.py` additions: the response carries `decision_id`; the policy stream record self-checks (use the reference to recompute rows) and the ledger pointer names the same decision id; a self principal is flagged. New `test_fleet_plan_policy.py`: ten consecutive plans with an exploring artifact choose the same target; the digest changes when the artifact changes. Ledger: **admit pointer + first policy stream records**.
  Verify: `cd node-py && python -m pytest tests/test_fleet_admit.py tests/test_hostd_admit_auth.py tests/test_fleet_plan_policy.py -q`.
- [ ] 3.3 Outcomes. On `hosted_release`, append (via `PolicyRuntime`'s Recorder, J§6.3 format) `lease_held_s` and `lease_expired=0`. On lease expiry/reap (find where `hosted_leases` entries are expired, in `hostbroker.py`), append `lease_expired=1` and `lease_held_s`. `POST /lease/{id}/release` accepts an optional JSON body `{status: "ok"|"failed", wall_s: number}`, which adds `caller_ok` (1/0) and `job_wall_s`. Invalid body values are rejected with 422; they are never coerced. Leases with no `decision_id` (e.g. from `/admit`) emit nothing. Size the policy stream per §9: measure the median policy record size, compute the days of retention at the average GRANTED rate from task 0.1 (≈ 885/h; refusals are not written) under 128 MiB × 16, and raise the bound (up to 8 GiB) if it is under 21 days. Write the arithmetic into the commit message; task 6.2 puts the values in the drop-in.
  Tests: `test_policy_outcomes.py`: release with and without a body; expiry; a bad body gives 422; a lease without a decision id emits nothing. Ledger: **adds `policy_outcome` records to the policy stream at release/expiry**.
  Verify: `cd node-py && python -m pytest tests/test_policy_outcomes.py -q`.
- [ ] 3.4 `node-py/livestack_node/workloads/lease_helper.py`: send `{status, wall_s}` on release (wall time measured around the workload). Keep release best-effort as it is today: a failed release still must not fail the workload.
  Tests: extend the lease_helper test (find it: `rg -ln lease_helper node-py/tests`) to assert the body is sent. Ledger: none directly (it feeds 3.3).
  Verify: `cd node-py && python -m pytest $(rg -l lease_helper node-py/tests) -q`.
- [ ] 3.5 Policy routes per §6: `PUT /fleet/policy/{policy_id}?role=active|shadow`, `GET /fleet/policy/{policy_id}`, `POST /fleet/policy/{policy_id}/revert` in `hostd.py`, plus the `policy_admin` principal capability in `fleet_auth` (token-file schema), atomic write (temp+fsync+rename, previous kept), 403 when fleet auth is off, 503 when the native validator is unavailable, 422 with all violations. The validator is INJECTABLE on PolicyRuntime (`validator=` callable returning `(version, violations)`); production uses `livestack_policy.load_artifact` when importable, tests inject a fake. `GET /fleet` includes `policy: PolicyRuntime.status()`.
  Tests: `test_policy_routes.py` covering every scenario of the spec requirement "Only a validated, authorised artifact changes routing" (auth off, native unavailable, out-of-bounds → 422 list, revert without a model) plus "a file that fails validation keeps the previous artifact". Ledger: none.
  Verify: `cd node-py && python -m pytest tests/test_policy_routes.py -q`.

## 4. Native crate (needs Jingway groups 1–4 merged)

- [ ] 4.1 Create `native/policy/` exactly as §4, pinned to the merged Jingway commit. Implement `ChooseTarget` per §2 with `ID = "livestack.fleet.choose_target"`, `VERSION = 1`, `MAX_EPSILON = 0.10`, and the param space of §2.3.
  Tests: `cargo test` in `native/policy`, covering one unit test per reason code and the four invariant fixtures of §4 (run through `livestack-policy replay --expect`). Ledger: none.
  Verify: `cd native/policy && cargo test --workspace && cargo run -p livestack-policy-cli -- families | jq -e '.[0].id=="livestack.fleet.choose_target"'`.
- [ ] 4.2 Build the Python module (`maturin develop -m native/policy/py/Cargo.toml` in a test venv) and write `node-py/tests/test_policy_differential.py` per §4 (10 000 cases, seed 20260924, mismatches written as fixtures). Skip it with a named reason if `livestack_policy` is not importable. The rollout checklist in 6.x requires it to have RUN, not skipped.
  Tests: that file. Ledger: none.
  Verify: `cd node-py && python -m pytest tests/test_policy_differential.py -q -rs` shows `passed`, not `skipped`.
- [ ] 4.3 Replay self-check. The policy stream is already in the J§6.2 format, so the CLI reads it directly and nothing needs extracting. Run `livestack-policy selfcheck --records '<dir>/livestack.fleet.choose_target.jsonl*'` against records written by a local `hostd` in a test (live records exist only after 6.2). Re-run it on a copied day of live records after 6.3.
  Tests: `node-py/tests/test_policy_selfcheck.py` (spawns the CLI over test-written records). Ledger: none.
  Verify: the test passes, and after 6.3 `receipts/selfcheck-<date>.md` shows `self_check: passed`.

## 5. Improver host in fleetd (needs Jingway groups 5–6 merged)

- [ ] 5.1 `fleetd/src/policy/source.ts`: `StreamPolicySource implements PolicyRecordSource` (J§9.1). It reads `$LIVESTACK_POLICY_DIR/records/livestack.fleet.choose_target.jsonl*` (rotated files included, oldest first) and yields decision records, outcome records and `recorder_gap` records. The improver runs on the broker host, so this is a local read. A missing rotated file inside the window is reported as a gap, never skipped silently.
  Tests: `source.test.ts` with fixture stream files, including a missing rotation and a `recorder_gap`. Ledger: none.
  Verify: `cd fleetd && npm test && npm run typecheck`.
- [ ] 5.2 `fleetd/src/policy/improver.ts` and the `npm run policy-improver -- --once` entry point. Each run:
  1. Open PGLite at `$LIVESTACK_POLICY_IMPROVER_DB`.
  2. Bootstrap: if the activation ledger has no `livestack.fleet.choose_target`, activate the artifact currently on the broker (`GET /fleet/policy/...`) as hand-authored.
  3. Reconcile: the broker's active version must equal the ledger's, else report `policy_projection_drift` and stop.
  4. Build the window (default: the last 7 days).
  5. Run Jingway `tune_policy` with objective and floors from §11 Q1 (defaults as written there, marked "proposed, pending a person" in the activation payload).
  6. Write any proposal as a document (Markdown file under `…/policy-improver/proposals/<id>.md` plus the ledger record).
  7. Evaluate shadow windows if shadows are active.
  8. Prune per §9.
  9. Print a one-screen summary that names every precondition that failed. A run that proposes nothing must say why.
  
  `PolicyPublisher` = an authenticated `PUT` to the broker, with the token from `$LIVESTACK_POLICY_ADMIN_TOKEN_FILE`.
  Tests: `improver.test.ts` against a fake broker (reuse `fakeBroker.ts`) and fixture streams: bootstrap, drift, a no-proposal run naming its reason, a proposal run on a synthetic ledger where a param change truly improves the objective. Ledger: none (it reads).
  Verify: `cd fleetd && npm test && npm run typecheck`.
- [ ] 5.3 `fleetd/src/policy/approve.ts` CLI: `npm run policy-approve -- <proposalId> [--shadow|--activate]`. It records `human_patch_approval` with the operator id from `$USER`, then PUTs the artifact as shadow or active. `--activate` refuses unless the proposal has passed the shadow rung. Also `npm run policy-revert -- livestack.fleet.choose_target`: the activation transition first, then `POST …/revert`.
  Tests: `approve.test.ts`. Ledger: none.
  Verify: `cd fleetd && npm test`.

## 6. Deploy (compare mode first)

- [ ] 6.1 Build the production artifacts on a Linux x86_64 host that is not the tower: `maturin build --release -m native/policy/py/Cargo.toml` produces an abi3 wheel. Unzip `livestack_policy.abi3.so` from it. Also run `cargo build --release -p livestack-policy-cli`. Check glibc: `objdump -T livestack_policy.abi3.so | grep -o 'GLIBC_[0-9.]*' | sort -V | tail -1` must be ≤ the tower's `ldd --version`.
  Tests: import smoke with the tower's interpreter version (3.12) in a matching venv on the build host: `python -c "import livestack_policy; print(livestack_policy.families())"`. Ledger: none.
  Verify: both artifacts exist and the smoke prints the family.
- [ ] 6.2 **[ASK]** Create a new release directory `~/.local/share/livestack-releases/scheduler-policy-<shortsha>/`, following the pattern of the existing releases named in the `livestack-fleetd` drop-ins (see `systemctl cat livestack-fleetd`). Put `node-py/` from this commit there, copy `livestack_policy.abi3.so` into `node-py/`, and copy the CLI into `bin/`. Write a NEW drop-in that points `PYTHONPATH` at it and sets:
  - `LIVESTACK_POLICY_NATIVE=compare`
  - `LIVESTACK_POLICY_RECORDS_MAX_MB` / `LIVESTACK_POLICY_RECORDS_FILES` (from 3.3)
  - `LIVESTACK_POLICY_DIR`
  - `LIVESTACK_POLICY_SELF_PRINCIPALS`
  
  Add a `policy_admin` principal to the fleet token file, following the token-file procedure in `openspec/changes/fleet-provisioning-activation/ACTIVATION.md` (never `Environment=` for secrets). Then `sudo systemctl daemon-reload && sudo systemctl restart livestack-fleetd`. Rollback is deleting the new drop-in and restarting. Write that down in the receipt before restarting.
  Tests: post-restart smoke from another host: `GET /fleet` shows `policy.source: "defaults"`, `native: true`, `mode: "compare"`; one `/fleet/admit` from a test principal returns a `decision_id`, and a J§6.2 record with that id exists in the policy stream, plus the pointer in the ledger. Ledger: first live policy records.
  Verify: the smoke outputs are pasted into `receipts/deploy-<date>.md`.
- [ ] 6.3 **[ASK]** After ≥ 7 days and ≥ 5 000 decisions in compare mode with `mismatches == 0` (or as many as the admit rate from 0.1 allows; if that is fewer than 5 000 in 7 days, report the actual count and ask), switch the drop-in to `LIVESTACK_POLICY_NATIVE=auto`. PUT the first artifact: today's defaults, `exploration.enabled=false`, `provenance.created_by: "human:<operator>"`. Re-run the 4.3 selfcheck on live data.
  Tests: selfcheck passed on ≥ 1 day of live records. Ledger: records now carry the artifact's version rather than the defaults' marker.
  Verify: `receipts/native-cutover-<date>.md` with the mismatch count, selfcheck summary and `GET /fleet/policy/livestack.fleet.choose_target` output.
- [ ] 6.4 **[ASK]** Install the systemd user timer `livestack-policy-improver.timer` (daily, 04:17 local, `Persistent=true`), which runs `npm run policy-improver -- --once` from the release's fleetd. Record its storage bounds in `_plans/decision-ledger.md` and add the §9 table there.
  Tests: one manual `systemctl --user start livestack-policy-improver.service`; its journal shows the summary. Ledger: none.
  Verify: `receipts/improver-first-run-<date>.md` containing the summary.

## 7. Turning it on (person-gated)

- [ ] 7.1 **[ASK]** Present §11 Q1 (objective and floors) to the operator, with the admit rate and days-to-2 000 from 0.1. Record the answer in the activation payload via a new hand-authored artifact revision. Changing only the objective in the activation payload does not change the artifact version.
  Tests: none. Ledger: none. Verify: `GET` the activation from the improver DB shows the confirmed objective and `approvedBy`.
- [ ] 7.2 **[ASK]** Present exploration (§5; recommended `epsilon=0.05`, `margin=0.25`) with its concrete cost: roughly 5% of admits that have a near-tied alternative go to the second-best running machine. On a yes, publish a new artifact with exploration enabled.
  Tests: 24 h later, from the ledger: the observed explored fraction is within ±50% of the expectation computed from the explore-set sizes (the positive control that exploration is actually happening); zero explored choices of non-running or LAST_RESORT targets. Ledger: explored decisions now carry propensities < 1.
  Verify: `receipts/exploration-first-day.md`.
- [ ] 7.3 First shadow window. When the improver produces its first proposal, approve it to shadow (`policy-approve --shadow`). After `shadowWindows` windows, record the shadow evidence. This receipt is the one the Jingway change's task 8.3 waits on.
  Tests: none new. Ledger: records carry `shadow` choices.
  Verify: `receipts/first-shadow-<date>.md`, sent to the Jingway change as well.

## 8. Docs and archive

- [ ] 8.1 Update the stale design records per `proposal.md`: add a status note to `_plans/fleet-scheduler.md` §5/§7 (resolve_weights unwired; the policy artifact is the shipped mechanism), add the outcome join to `_plans/decision-ledger.md` §3, and add a "Scheduler policy" section to `HARMONY.md` (operator reference: routes, modes, revert, where files live, how to read `/fleet` policy status).
  Tests: none. Ledger: none. Verify: `rg -n "choose_target" _plans/fleet-scheduler.md _plans/decision-ledger.md HARMONY.md` finds all three.
- [ ] 8.2 `openspec validate scheduler-policy-routine --strict`, then after 7.3 `openspec archive scheduler-policy-routine`.
  Verify: `openspec validate --specs` exits 0.
