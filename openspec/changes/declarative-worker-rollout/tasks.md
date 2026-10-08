## 1. Claims and reload (phase 0; no rollout behaviour yet)

- [x] 1.1 `claims.py` + `schema.sql` `worker_claims`; routes `GET claims`, `POST claims/<w>/drain|enable` with CAS `generation`, required expiry, owner, `drain_held_by`. Tests: real-HTTP/SQLite for conflict, expiry cap, forced drain. Ledger: `drain`, `enable`, `drain_expired`. **Done (round 1).**
- [x] 1.2 Placement, assignment and capacity read claims from the store in one set-based query. Tests: statement count independent of worker count (4 vs 40); existing attempts keep access; `worker_draining` reason unchanged. **Done (round 1).**
- [x] 1.3 One-time import of `claim_enabled` from `authority.json` for workers without a row; `claim_enabled_in_file_ignored` log afterwards. Tests: import once, later file edit ignored, reload with torn file. **Done (round 1).**
- [x] 1.4 Lazy + 30 s tick expiry; `needs_operator` exception. Tests: expiry re-enables; `needs_operator` does not. **Done (round 1).**
- [x] 1.5 `GET reload/status` (applied time, hash, file hash, refusal). Tests: edited-not-signalled shows mismatch; refusal reason surfaced. Update `authority-principal-reload.md` with the explicit SIGHUP-only rule and state-not-reloaded list. **Done (round 1).**
- [x] 1.6 CLI `workload drain --until/--ttl`, `enable`, `claims`, `claims export --authority-json`. Tests: CLI against a real authority. **Done (round 1).** CLI is `python -m livestack_node.workloads.cli drain|enable|claims|reload-status|rollout|unit`.

## 2. Deployment unit

- [x] 2.1 `unit.py`: `deployment-unit.v1` schema (closed keys, credential-like field refusal), digest, `workload unit build` (fails on missing part, capture over cap, offline handler import failure). Tests: each failure; digest stability. Ledger: none (build-time). **Done (round 1).**
- [x] 2.2 Worker report: part digests under facts `unit` (within the 8 KiB facts bound). Tests: size bound, old worker without it is `unknown`, `off` leaves reports byte-identical. **Done (round 1).** Opt-in per worker (`report_unit`): an older authority refuses the unknown key.
- [ ] 2.3 Roster `unit_state` (`current|behind|unit_mismatch:<part>|unknown`) and placement filter per part. Tests: new handler + old verifier excludes compilation only. Ledger: roster state. **Partial (round 1): roster `unit_state` done; PLACEMENT FILTER per part not done (report-only this round).**
- [x] 2.4 Capture-size check at build, plus runtime check reused by the `capture_size` probe. Tests: over/under cap. **Done (round 1).**

## 3. Rollout spec and pure planner

- [x] 3.1 `rollout-spec.v1`, store with generation, `POST rollout/spec` CAS, `GET rollout`, export to file. Tests: race, unknown key, min_claiming zero. **Done (round 1).**
- [x] 3.2 `rollout.py` `plan(spec, roster, claims, now)` pure, table-driven from the incidents (a)-(d): min_claiming, one-at-a-time, hold, stale, canary choice and `canary_not_representative`, pause after two failures, attempt caps. Tests: pure tables. Ledger: action schema. **Done (round 1).**
- [x] 3.3 Drift report in roster and `workload rollout status` (per set: unit, state, waiting reason). Tests: against live-shaped fixture of 2026-10-08 skew (image handler release on e2e-1 vs 3/4/5; `e2e.task` only on e2e-2). **Done (round 1).** Observe-mode reconciler reports drift; live 2026-10-08 skew captured.
- [ ] 3.4 Mode `off|observe|enforce` and kill switch; `observe` ledgers intended actions only. Tests: observe touches no claim, no worker. **Partial (round 1): modes `off|observe` done; `enforce` is refused by the spec validator (operator-only, no executor).**

## 4. Stage, activate, rollback mechanics

- [ ] 4.1 Worker `desired_unit` in sync response only while a step is active; digest-verified download into a new directory; atomic `current` symlink swap; retain previous. Tests: real process, interrupted download resumes, wrong digest refused.
- [ ] 4.2 Clean exit after idle for release change so supervisor restarts on new path; handler-only change uses registry activation with no restart. Tests: real systemd-run transient unit if available else subprocess supervisor; no running attempt killed.
- [ ] 4.3 Verifier refresh helper contract: path-activated root unit selecting among operator-staged payloads, digest verified; absence yields `verifier_manual`. Tests: root-in-container or transient service; unverifiable digest refused. Builds on CDH 5.1 `enroll_worker.py`.
- [ ] 4.4 Rollback: auto on crash loop, smoke failure, failure budget; `needs_operator` on rollback failure. Tests: injected bad release restores previous; failed rollback stays drained after expiry.
- [ ] 4.5 Retention of previous releases/digests (24 h after fleet-wide current), bounded disk. Tests: bound enforced, in-use never reaped.

## 5. Smoke

- [ ] 5.1 `smoke.py` probe vocabulary and runner pinned to one worker via internal `rollout_smoke` principal, lowest priority, <= 5 min. Tests: placement pin, no customer work displaced. **Partial (round 1): probe vocabulary, runner and CLI done; the `rollout_smoke` principal job runner pinned to one worker is enforce-mode work, not done.**
- [x] 5.2 Probes: `worker_restart_clean`, `handler_import`, `handler_integrity`, `rootless_docker_start`, `compilation_launch`, `capture_size`, with `not_applicable` and named failures. Tests: one failing fixture per past incident: undefined name and redeclaration; placeholder removed before integrity; user-id mapping failure (simulated newuidmap EPERM); stale verifier copy; oversize capture. **Done (round 1).** Python static + `node --check`; JS undefined identifier needs eslint on the host (reported as not_checked).
- [ ] 5.3 Soak window and post-enable failure budget attributed by reason code. Tests: unit-attributed failures trigger rollback, unrelated job failures do not.

## 6. Reconciler loop

- [ ] 6.1 Tick wiring executes planner actions with ledger records and bounded rollout-run table (count + age). Tests: restart of the authority mid-step resumes or safely abandons; never double-drains.
- [ ] 6.2 Approval gate for signer/keystore workers (`awaiting_approval`), publish-stage check. Tests: no activation without approval.
- [ ] 6.3 Static test: no key material in spec, unit, descriptor, smoke job (extends CDH 4.3).

## 7. Migration and docs

- [ ] 7.1 Phase 1: observe on the live fleet; hand-built unit for today; file each skew as fixed or intentional. (Operational.)
- [ ] 7.2 Phase 2: move host unit files to the `current` symlink, e2e slot first. (Operator-only.)
- [ ] 7.3 Phase 3: `enforce` on `zz-joe-e2e` with three real rollouts including a deliberately bad unit that must be caught and rolled back. (Operational evidence recorded in the docs.)
- [ ] 7.4 Phase 4: `zz-joe-release` outside publishing hours with the operator present. (Operational.)
- [ ] 7.5 Docs: rewrite `worker-release-rollout.md` around the declarative path, keep the manual runbook as the operator-only fallback; update `authority-principal-reload.md`; `_plans/durable-workloads.md` deployment-unit section.

## 8. Reconcile with capability-driven-handlers

- [ ] 8.1 Amend CDH 3.8 and 5.6 to run through this change's canary rather than hand canaries; CDH 5.1 `enroll_worker.py` exposes the apply entry point used by 4.3.
- [ ] 8.2 Benchday companion: bundle builder emits the unit manifest (fold into CDH 6.3).
- [ ] 8.3 `openspec validate --specs` and `--changes` green; archive after phase 3 is live.
