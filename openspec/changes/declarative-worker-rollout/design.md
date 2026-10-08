## Context

Today a rollout is: build a release dir (`worker-release-rollout.md`), copy it to each host, edit the unit drop-in, set `claim_enabled:false` in `authority.json`, SIGHUP, wait for idle, restart the unit, set `claim_enabled` true, SIGHUP. The handler bundle and the root-owned verifier copy are separate hand deploys. Facts used here:

- `claim_enabled` is a field of the worker `Principal` (`http.py:46`), read from the file by `reload_principals` on SIGHUP only (`service.py:26-60`, `:149`). It is consulted by placement (`placement.py:128`), assignment (`http.py:489`, reason `worker_draining`) and handler capacity (`http.py:444`).
- Principals, `handlers`, `handler_release_policy`, `environment_handlers` reload live; `state_dir`, `port`, `limits` do not (`authority-principal-reload.md`).
- The handler registry already stages immutable digests and activates atomically without a core restart (`workload-handler-releases`, `handler_registry.py`, `handler_installer.py`, `handler_sync`). The registry is the right substrate for handler bundles; it lacks only a *when* and a *who first*.
- Verifier copies live at `/opt/livestack-compilation/<worker>/` and are root-refreshed; CDH D-section proposes `enroll_worker.py` and a per-host verifier. Both are inputs here.
- CDH `facts` (<= 8 KiB, closed keys) is the worker-to-authority channel; this change adds keys to it rather than a second channel.

## Goals / Non-Goals

Goals: (1) no worker runs bytes nobody declared; (2) a drain has an owner and an end; (3) no change reaches more than one worker until a real launch has succeeded on that one; (4) the three parts of a fix travel as one thing; (5) every automatic action is ledgered, bounded, and has an off switch.

Non-goals: choosing which handlers a worker serves (CDH); changing handler immutability; provisioning machines (fleet-provisioning-*); rolling the authority itself (stays operator-only); a general deployment system for non-Livestack software; Windows/macOS reconciler support in v1 (they get drift reporting and manual canary, same gating as CDH 5.7).

## Decisions

### D1. Desired state is data, in the authority, schema-validated

A `rollout-spec.v1` document (closed keys, extra=forbid, <= 64 KiB) holds `sets` and `workers`. A **capability set** is the group of workers sharing a role (e.g. `zz-joe-e2e`, `zz-joe-release`, `apple`), declared by selector over roster facts and labels, so same-role peers cannot drift apart silently. Per set: `unit` (a deployment-unit id, D4), `min_claiming` (integer >= 1, or fraction), `canary` (worker id or `auto`), `smoke` (probe list, D3), `window` (optional allowed hours), `max_unavailable` (default 1). Per worker override: `unit` pin, `hold: true` (never touched by the reconciler). The spec is stored in the authority DB with a monotonically increasing `generation`; submitted through `POST rollout/spec` with `if_generation`, so two writers cannot overwrite each other (same compare-and-swap as D2). A copy is exportable to a file for review and committed to a repo by the operator; the file is not the source of truth at runtime.

Alternative rejected: keep a file and add file locking. It does not help across hosts, and the failure was semantic (read-modify-write of a whole document), which only versioned writes fix.

### D2. Claims are authority state with CAS and TTL

New table `worker_claims(worker, enabled, generation, owner, reason, expires_at, updated_at)`. API (operator or `rollout` principal only):

- `GET claims` and `GET claims/<worker>`.
- `POST claims/<worker>/drain {owner, reason, ttl_seconds | until, if_generation}` and `.../enable {owner, if_generation}`. `ttl_seconds` is required, max 24 h (configurable cap); an unbounded drain is refused by name `drain_requires_expiry`. A second drain by another owner while one is live is refused `drain_held_by:<owner>` unless `force` (operator only, ledgered).
- An expired drain re-enables the worker lazily (evaluated at claim and at a 30 s authority tick) and writes a ledger `drain_expired` record. CLI: `workload drain <worker> --until 2026-10-09T06:00Z --reason ...`, `workload enable`, `workload claims`.
- `claim_enabled` in `authority.json` becomes only the *initial* value, imported once when the worker has no row (migration, D8). After that the file value is ignored and the reload log says `claim_enabled_in_file_ignored:<worker>` so no operator is surprised. Placement, assignment and capacity read the store (one set-based query per request, bounded independent of worker count, per Benchday rule 14 spirit).
- In-flight attempts keep access exactly as today ("existing attempts retain access").

Reload semantics, made explicit and tested: (i) the authority never watches the file; SIGHUP is the only trigger; (ii) the rollout/claim/spec state is **not** reloaded from files at all; (iii) `reload` returns a success/refusal that is also exposed at `GET reload/status` (last applied time, hash, refusal reason) so an operator can tell "applied" from "silently not read". That removes the one-hour unknown.

### D3. Canary and smoke

For a set whose desired unit differs from what its workers report, the reconciler picks the canary (the declared one, else the lowest-id idle worker not in `hold`). Sequence:

1. **Stage** the unit parts on the canary (D6 mechanics), still serving the old parts.
2. **Drain** the canary (D2, owner `rollout`, TTL = `stage_timeout + 15 min`).
3. **Wait idle** (no running attempt, never kill), **activate**: restart the worker unit on the new release if the release part changed; registry activation if only the handler bundle changed; verifier refresh if only the verifier changed.
4. **Smoke** while still drained: the authority submits smoke jobs pinned to that worker through the normal placement with an internal `rollout_smoke` principal. They are real launches, not a ping. Closed probe vocabulary (extends CDH probe vocabulary):
   - `worker_restart_clean` (unit active, no crash-loop within 60 s)
   - `handler_import` (import and syntax check of every handler in the bundle on this worker's interpreter, catches the `cacheComponents is not defined` / `workerCache` redeclaration class in under a second, not after 25 s of a real attempt)
   - `handler_integrity` (the placeholder/integrity check runs against the *bundle as shipped*, catches the removed-placeholder class)
   - `rootless_docker_start` (start and remove a trivial container as the worker's user, including the `newuidmap` path; catches the `PrivateTmp=yes` class)
   - `compilation_launch` (a no-op compilation launch through the root verifier; catches a stale or missing verifier copy)
   - `capture_size` (the produced runtime capture is under the cap; this check also runs at build, D4)
   Each probe has a time bound and a named failure `smoke_failed:<probe>:<reason>`. A set may require a subset but the minimum is `worker_restart_clean` + `handler_import` + `handler_integrity`; probes that need facts the worker lacks (no rootless docker) are `not_applicable`, never silently passed (unknown is not success).
5. **Pass:** re-enable the canary, record `canary_passed`, wait `soak` (default 10 min, per set) in which real job failures attributable to the unit (`handler_import_error`, launch refusals) count against it, then fan out (D5). **Fail or timeout:** roll the canary back to its previous unit (D7), re-enable it, mark the unit `rejected` for that set with the failing probe name, and stop. Nothing else is touched.

A smoke job consumes real capacity; it is bounded (<= 5 min, one per canary per unit) and never queues ahead of customer work (lowest priority class, placed only because the worker is drained to everything else).

### D4. The deployment unit

`deployment-unit.v1` manifest, immutable and content-addressed (`unit-<sha8>`): `release` (existing `livestack-<sha8>` digest from `RELEASE.json`), `handlers` (list of registry digests, the bundle), `verifier` (digest of the root-owned verifier copy payload, optional for sets without compilation), `capture` (size in bytes and cap), `min_authority` (authority version needed), `built_from` (git commit(s)). Built by one command (`workload unit build`) that fails if any part is missing, if the capture size exceeds its cap (area d), or if handler import fails offline. Reuse, don't duplicate: release dir format stays; handler digests stay registry digests; verifier payload is what CDH `enroll_worker.py` already lays down.

Each worker reports `unit` in facts: ids it actually runs for each part, plus `unit_id` only if all three equal one manifest. The roster computes `unit_state` per worker: `current`, `behind`, `unit_mismatch` (parts not from one unit, e.g. new bundle, old verifier), `unknown` (legacy worker, no facts: shown as such, still serves). `unit_mismatch` withdraws eligibility only for work requiring the mismatched part (verifier mismatch blocks compilation placement, not `release.hub`). This is a report and a placement filter, not a kill.

Root-owned verifier refresh remains a privileged step: the worker does not gain root. Mechanism: the reconciler requests it through the `enroll_worker.py --apply <unit>` helper running as a root-owned systemd unit triggered by a path/socket the worker can poke but whose input is only a unit digest it verifies against the authority's signed unit manifest. This keeps "what root runs" operator-installed once; the unit digest selects among payloads already staged in a root-owned directory. If an operator has not installed the helper, the set is `verifier_manual`: the reconciler stages and reports "waiting for operator" instead of guessing.

### D5. Reconciler: order, bounds, stop conditions

Runs in the authority as a tick (30 s) with a pure planner `plan(spec, roster, claims, now) -> actions` (no I/O; table-tested). Rules:

- One worker per capability set in a non-`current` active step at a time (`max_unavailable` default 1), and a step may not start if it would leave fewer than `min_claiming` claiming and idle-or-busy-but-enabled workers in the set. Unmet minimum: `waiting: min_claiming` in the roster, never a forced step.
- Fan-out order after canary pass: the rest, lowest-load first, each repeating drain, idle wait, activate, short smoke (restart_clean + import + integrity only), enable. Two consecutive failures in the set pause the rollout (`paused: failures`), enable everyone, and require an operator to resume.
- Worker unreachable, report stale, or `hold`: skipped and reported, never retried in a loop; per-worker attempts capped (3 per unit) then `needs_operator`.
- Global kill switch `rollout.mode: off | observe | enforce`. `observe` computes and ledgers what it *would* do and reports drift, touching nothing.
- Every action writes a ledger record (`rollout_action`: set, worker, unit, step, result, reason) bounded by the authority's ordinary retention; rollout runs table bounded by count and age (Benchday rule 10 analog).

### D6. How a worker is told to stage and restart

Workers already poll the authority (`worker/report`, `handler_sync`). Add `desired_unit` to the sync response for that worker only when a step is active for it. The worker: downloads release/verifier parts via existing blob routes with digest verification into a NEW directory (never in place; matches `windows-worker.md` and the manual runbook); installs handler parts through `handler_installer.py`; for a release change, exits cleanly after idle so its supervisor (systemd `Restart=always`, launchd, Windows service) starts the new `PYTHONPATH`. The path switch is a single atomic symlink swap (`current -> livestack-<sha8>`), with the previous symlink target kept (for rollback D7). This needs one-time unit file change (point `PYTHONPATH` at `current`); that change is operator-only and is the migration gate per host.

### D7. Rollback

Per worker, automatic, bounded: the previous symlink target and previous registry digests are retained until the next unit is `current` fleet-wide for 24 h (disk bound per retention rules). Failure at any step after activation (restart crash-loop, smoke fail, post-enable failure budget: >= 3 attempt failures with a unit-attributed reason within 10 min) triggers: drain, swap back, restart, `worker_restart_clean` smoke, enable, mark unit `rejected` for the set. Rollback of the rollback fails closed: worker stays drained with `needs_operator` and a TTL-less hold is NOT allowed, so it shows on the roster and via `needs-you` instead of expiring into service while broken (an expired drain on a worker in `needs_operator` does not re-enable; this is the one exception to D2 auto-enable and is stated in the spec).

Set-level rollback: `workload rollout revert <set>` re-declares the previous spec generation (CAS) and the reconciler converges back through the same canary path.

### D8. Migration (each step independently reversible; mode `off` undoes the whole feature)

0. Ship claims API and tables, importer, `reload/status`. File `claim_enabled` still works for rows that do not exist yet. Operators may start using `workload drain --until`. Behavior of everything else unchanged.
1. Ship unit report in facts and roster `unit_state`, rollout `observe`. Hand-build a unit manifest for what runs today; diff reality vs it; expected to expose existing skew (a above). Fix skew by hand using the old runbook, now with a precise list.
2. Switch hosts to the `current` symlink unit file, one host class at a time, starting with a non-publishing e2e slot. Manual drain via new API.
3. `enforce` for one low-risk set (`zz-joe-e2e`), with `canary` chosen explicitly. Run at least three real rollouts including one deliberately bad unit (a handler with a syntax error, in a scratch set) that must be caught by smoke and rolled back.
4. Extend to `zz-joe-release` only with `window` set outside publishing hours and an operator present for the first run.
5. Remove file-based `claim_enabled` from docs; keep import for one release for old configs.
Rollback of the change: set mode `off`; claims remain authoritative but a documented one-liner (`workload claims export --authority-json`) writes them back to the file format.

### D9. Operator-only (not automated, by decision)

- Authority code upgrade and restart (needs a fully idle window; existing rule).
- Anything requiring root beyond the pre-installed enrolment helper: first install of the helper, new worker slot enrolment, sudoers, systemd unit-file edits (including the migration to `current`).
- Changing `rollout.mode` to `enforce`, the minimum probe set, `min_claiming`, and the TTL cap.
- Releases touching signer/keystore handlers (`release.hub`, `release.app.android`): the reconciler stages and canaries but activation on those workers waits for an explicit operator `approve` (CDH access levels), and never while `publish.sh` has a stage on that worker.
- Forcing a drain held by another owner; resuming a paused rollout; clearing `needs_operator`.
- Windows and macOS worker restarts (v1: stage + report only).
- Spending decisions, credentials, and secrets: nothing in a unit manifest, desired spec, descriptor or smoke job carries key material (same static test as CDH 4.3).

## Risks / Trade-offs

- **A reconciler that restarts machines is a new outage source.** Mitigated by observe mode first, one-at-a-time, `min_claiming`, failure budget pause, kill switch, and the rule that an uncertain state means skip-and-report. It never kills a running attempt.
- **The smoke job is only as good as its probes.** The four incidents each map to a named probe, but a new class of break will pass. Mitigation: post-enable failure budget (D7) is the second net; every incident adds a probe, which is a one-line vocabulary entry plus test.
- **Authority becomes more stateful and a tick owner.** The tick reads bounded sets; tables are bounded and ledgered; a dead tick means no rollout, not a bad one.
- **TTL auto-enable can re-expose a worker someone meant to keep out.** Deliberate: the observed failure is the opposite (forgotten drains). The `needs_operator` exception and the roster `drain` column with owner and expiry cover the intentional case; a long hold is an explicit `hold: true` in the spec, which is visible and reviewed.
- **Canary of one may not represent the set** (e.g. only `-2` has `e2e.task`). The planner chooses a canary that serves every handler in the set when one exists, else reports `canary_not_representative:<handlers>` and requires an operator-chosen canary.
- **Compatibility:** old workers without facts get no unit state and no reconciliation (`unknown`); they keep working and are rolled by hand until upgraded once.
- Estimate: roughly 40 tasks, about 5 engineer-weeks plus observation windows (steps 1 and 3).

## Owner decisions (2026-10-08, resolving the open questions)

1. The reconciler runs as a SEPARATE service principal (`rollout`) with a smaller blast radius, calling the authority API; it does not run inside the authority tick. The authority keeps only claim expiry.
2. Verifier refresh: a root-owned helper MAY install payloads the owner has pre-staged. It installs only content-addressed payloads whose sha256 appears in a root-owned allow file, and nothing else. This removes the third deploy path.
3. Auto-re-enable on drain expiry is accepted. Intentional long holds use an explicit `hold: true`.
4. `min_claiming`: 2 of 5 for the zz-joe e2e slots; at least 1 for every other capability set (release workers: 1 of 2).
5. Soak 10 minutes and failure budget 3 are the starting defaults.
6. Runtime capture cap: this change only makes exceeding it a build-time failure. Raising or splitting stays a separate decision.
7. Enforce mode is operator-only. Round 1 implements phases 0-1 and reconciler OBSERVE only.
