## Why

Handlers, worker releases and the root-owned compilation verifier are rolled to the fleet by hand, one worker at a time, with ad-hoc scripts, and nothing checks a change on one machine before it reaches all of them. The overnight session of 2026-10-07/08 produced four families of failure. Each is a missing capability, not an operator slip.

**(a) Legacy bundles pinned per worker and drifting.** `zz-joe-release` ran an Oct 4 handler bundle that lacked cross-target support, so every arm64 build on it failed while the same handler on other workers passed. Labels skew between same-role peers: `benchday_image_handler_release` differs on `zz-joe-e2e-1` versus `-3/-4/-5`, and `benchday.e2e.task.v1` exists only on `-2`. `capability-driven-handlers` already shows the same shape for handler *lists* (its table of `rust-check` on `-2`/`-5` only). It does not cover the *content and version* a worker runs.

**(b) Hand-driven drain and roll.** The only drain is `"claim_enabled": false` in `~/.config/livestack-workloads/authority.json`, applied by SIGHUP (`node-py/docs/worker-release-rollout.md`, "Drain and idle rule"; `service.py:reload_principals`). Consequences seen: several workers left `claim_enabled: false` after a roll that was interrupted or forgotten; two agents each read the whole file, edited one principal and wrote it back, and the second write erased the first; and whether the authority re-reads the file by itself was unknown for an hour (it does not; only SIGHUP, `service.py:149`). A drain is a bit in a shared file with no owner, no expiry and no compare-and-swap.

**(c) Broken releases reaching every worker before anyone noticed.**
- `5cd61923` set `PrivateTmp=yes` in the worker unit; on every `--user-manager` worker rootless docker could no longer start (`newuidmap` EPERM). Found by failing real jobs, after fan-out. (Follow-up: `privatetmp-rootless`.)
- A handler merge left `cacheComponents is not defined` and a redeclared `workerCache`; every attempt failed in about 25 s on every worker that took the bundle.
- A handler change removed a placeholder *before* the integrity check, so the check could not pass.
- A compilation-verifier fix needed three separate deploys: the worker release, the handler bundle, and a root-owned per-worker copy of the verifier under `/opt/livestack-compilation/<worker>/` that only root can refresh. Any one missing yields a worker that is "up" and refuses launches.

**(d) Capture size.** The runtime capture cap was exceeded, which forced release branches instead of a normal rollout. A size limit is a property of the deployment unit and should be checked at build, not discovered at publish.

The common cause: the system has no model of *what each worker should be running*, no safe way to take a worker out of rotation, and no step between "built" and "every worker".

## What Changes

1. **Declared desired state.** A schema-validated per-worker (and per-role) document names the worker release, handler bundle, verifier copy and drain policy it should run. A **reconciler** converges workers to it: stage, drain, restart when idle, health-check, re-enable. One worker at a time per capability set, never below a declared minimum still claiming. The roster reports drift.
2. **Claims through an authority API.** Drain and re-enable move out of the shared file into an authority-owned store with compare-and-swap (`generation`) and an owner and TTL on every drain. A drain expires and re-enables itself; `drain --until` exists. SIGHUP semantics are written down and the state that matters (claims) no longer depends on them.
3. **Canary and smoke.** A new release, handler bundle or verifier activates on exactly one canary worker per capability set first. A *smoke job*, a representative real launch (rootless docker start including `newuidmap`, a compilation launch through the verifier, handler import and syntax check, integrity check), must pass before fan-out. A failure rolls the canary back automatically and names the failing probe. No fan-out proceeds on a failed or missing smoke result.
4. **One versioned deployment unit.** A `deployment-unit` manifest binds worker release digest, handler bundle digest and verifier copy digest (and the capture-size check) under one id. Workers report the unit they run. A worker whose three parts do not belong to one unit is reported as `unit_mismatch` and is not eligible for work needing the mismatched part.
5. **Explicit operator-only boundary, migration and rollback** (design.md).

Relationship to `capability-driven-handlers` (CDH): CDH decides *which handlers a worker should serve* (assignment, facts, access levels, per-host verifier enrolment). This change decides *which code and policy bytes serve them and how those bytes arrive safely*. It **consumes** CDH's assignment as input to the desired state, **reuses** CDH's `facts` block (adds `unit` and `smoke` entries), and **reuses** its enrolment tool (`enroll_worker.py`) as the verifier stage step. It does not duplicate eligibility, requirements tiers or access levels. Where CDH task 3.8 ("canary `open` on `zz-joe-e2e-3`") and 5.6 ("zz-joe cutover plan") describe one-off canaries, this change supplies the general mechanism; those tasks are amended to use it (tasks section 8).

Design record realised: `node-py/docs/worker-release-rollout.md` (the manual runbook; becomes the description of operator-only fallback) and `authority-principal-reload.md` (reload semantics). `_plans/durable-workloads.md` should gain the deployment-unit section; it currently has no notion of a release as a unit.

## Capabilities

### New
- `worker-rollout`: desired state, reconciliation, claim control, canary and smoke, deployment unit, drift reporting.

### Modified
- `workload-handler-releases` (existing): activation gains a canary stage; see delta in `specs/worker-rollout`. No change to immutability or digest rules.

## Impact

- `node-py/livestack_node/workloads/`: new `rollout.py` (pure planner), `claims.py` (store + API), `unit.py` (manifest, mismatch evaluation), `smoke.py` (probe vocabulary and runner); touches `http.py` (claims and rollout routes, `claim_enabled` becomes derived), `service.py` (initial claims import from file, reload semantics), `roster.py` (drift and unit columns), `store.py` + `schema.sql` (claims, rollout runs), `worker.py` (report `unit`, accept stage/activate commands).
- Operators: `authority.json` stops being where drains live. A one-release compatibility import keeps old files working.
- Benchday companion: bundle builder emits the unit manifest (extends CDH task 6.3).
- Risk surface: a reconciler that restarts workers is itself dangerous; the design bounds it (design.md D5, D7).
