Estimate: ~36 tasks, about 4 engineer-weeks of code and tests plus the observation windows (phases 0, 2, 3 each need days of real traffic). Tests are real-HTTP/SQLite or real-process unless marked pure; no hand-rolled fakes of the authority. "Ledger" names the durable record each task leaves.

## 1. Pure evaluator and schema (no behaviour change)

- [ ] 1.1 `eligibility.py`: `evaluate(profile, release, facts, principal, policy)` returning `(state, reason_code, sentence)`. Tests (pure, table-driven from the live matrix of 2026-10-08): each reason code fires, strictest-tier wins, reserved labels ignored, unknown tool is `tool_unknown`. Ledger: none (pure).
- [ ] 1.2 Manifest `harmony-handler-package.v2` with closed `requirements`; v1 still accepted. Tests: v1/v2 digest stability, unknown keys refused, package cannot carry `access`/`compilation_classes`. Ledger: registry release event on stage.
- [ ] 1.3 Extend `HandlerReleasePolicy` / `parse_policy` with `access`, `compilation_classes`, `requires`, `execution`, `attested_labels`; extra=forbid; conflict with typed `compilation_handlers` refuses load. Tests: bad values fail closed without echoing input; SIGHUP reload keeps the previous set on error. Ledger: policy revision in events.
- [ ] 1.4 `runtime_discovery.py`: closed tool-probe vocabulary (`rootless_docker`, `cargo`, `flutter`, `android_sdk`, `chrome`, `docker_native_frontend`), bounded, each returns path/version or reason. Tests: real probes on a host with and without the tool (stub PATH dirs with real executables). Ledger: probe outcomes logged per start.

## 2. Facts and observe mode (phase 0)

- [ ] 2.1 Worker `facts` block and `facts_version`; `handler_assignment` config (`off` default, `observe`). Tests: report size bound, closed keys, `off` produces byte-identical reports to today. Ledger: none (report).
- [ ] 2.2 `store.register` accepts and bounds `facts`; handler id validation uses config union registry ids. Tests: refused oversize/unknown key retains previous facts; old worker without facts registers. Ledger: facts stored in the `workers.report` row (existing, bounded).
- [ ] 2.3 Authority computes and memoises assignment per worker (`hash(facts, policy_revision, generation)`). Tests: statement-count independence from worker/job count (4 vs 40 workers); recompute only on change. Ledger: bounded `handler_assignment_events` ring (256), enforced by delete-oldest.
- [ ] 2.4 Roster matrix and `workload handlers matrix`, `override_serving`, `facts_unavailable`; extend `GET handlers/<h>/capacity` with eligible-not-installed and reason histogram. Tests: `test_workload_roster.py` style with real HTTP; every cell has a state and reason. Ledger: none (read).
- [ ] 2.5 Phase-0 exit report: run observe on the live fleet, diff `would_serve` against today's lists (table above), file each difference as intentional (profile entry) or gap. Tests: n/a (operational). Ledger: report committed under `_plans/`.

## 3. Assignment, install, self-test, withdrawal (phases 2-3)

- [ ] 3.1 `desired()` takes the authority's assigned set, not `report['handlers']`; response carries `assigned`. Tests: assigned handler outside worker mode is not offered; pins unaffected. Ledger: assignment event.
- [ ] 3.2 Worker installs assigned handlers via existing installer; `handler_id_not_authorized_by_worker` becomes "explicit pin, or assignment mode permits this level, and local evaluation agrees". Tests: worker refuses an assignment its own evaluation rejects (`authority_assigned_but_worker_refuses`). Ledger: worker activation failures (existing, bounded 16).
- [ ] 3.3 Selftest execution through the handler's own backend (zero-input supervised attempt, bounded by `selftest.max_seconds`), result in report; quarantine; previous digest keeps serving. Tests: real worker with a failing selftest package; pointer not committed. Ledger: `handler_selftests` in report (bounded 64).
- [ ] 3.4 Withdrawal after running attempts end; `withdrawn` reason; no kill. Tests: capacity lowered mid-attempt, attempt completes, then handler leaves report. Ledger: assignment event.
- [ ] 3.5 Flap control: <= 8 changes/worker/hour, debounce; `assignment_rate_limited`. Tests: oscillating probe does not reinstall. Ledger: assignment event.
- [ ] 3.6 `deny` in `handler_policy`; precedence deny > pin > computed. Tests: all three combinations. Ledger: roster state `denied_by_worker`.
- [ ] 3.7 Per-handler `execution` from profile pushed in the descriptor; worker may only tighten. Tests: a worker block with larger `max_seconds` is clamped and says so. Ledger: activation event.
- [ ] 3.8 Canary `open` on `zz-joe-e2e-3`, then fleet `open`. Tests: week of identical job outcomes (operational). Ledger: rollout note in `docs/worker-release-rollout.md`.

## 4. Access levels (phase 3 continued, phase 5)

- [ ] 4.1 `host_enrolled`: require policy host classes plus `facts.verifier == enrolled`; `host_not_enrolled:<class>` / `verifier_missing` / `verifier_unreachable`. Tests: real policy file, class missing, expired policy withdraws. Ledger: assignment event with policy revision.
- [ ] 4.2 `host_attestations` in operator policy; reserved-label override at register (observe first: record `reserved_label_ignored`, enforce on flag). Tests: unattested signer label dropped, placement selector no longer matches; attested matches. Ledger: bounded event.
- [ ] 4.3 Prove no credential path: static test that sync responses, descriptors and manifests contain no key material fields; packages with credential-like filenames flagged at stage. Tests: sync payload schema is closed. Ledger: stage refusal event.

## 5. Verifier enrolment and per-host verifier (phase 1 and 4)

- [ ] 5.1 `enroll_worker.py`: `--all`, `--check`, idempotent, drift refusal; subsumes `zzops/scripts/stage_compilation_verifier.py`. Tests: real root-in-container or `systemd-run` transient service (same technique as the zzops fixture); second run changes nothing. Ledger: tool log line per artifact.
- [ ] 5.2 Worker `verifier` fact from `--check` logic (registry entry present, socket answers with uid 0 peer). Tests: missing, unreachable, enrolled. Ledger: fact in report.
- [ ] 5.3 Authority verifier principal (`verifier_host`) limited to `verify-compilation`, host-bound. Tests: cross-host worker refused, every other route refused. Ledger: refusal reasons in job/attempt reasons (existing).
- [ ] 5.4 Host mode in `launch_verifier.py`: host file, enrolled uid set, per-request derivation, threaded server with per-request monotonic deadline (no process-wide SIGALRM), atomic SIGHUP reload. Tests: the seven-check suite re-run against host mode (identity claim from foreign attempt, unenrolled uid, journal mismatch, post-authority containment change, resource caps, concurrent requests, reload with a bad file keeps the old). Ledger: one log line per admission/refusal (existing format plus `worker`).
- [ ] 5.5 Consumer: confirm many ids -> one socket needs no change in `launch_contract.py`; test with registry fragments pointing at the same path. Tests: real socket. Ledger: none.
- [ ] 5.6 zz-joe cutover plan (per-slot units kept one release cycle), then retire. Tests: `harmony_launch_fixture` control against host mode. Ledger: rollout note.
- [ ] 5.7 (later, own gates) macOS launchd and Windows multi-SID pipe variants.

## 6. Migration and docs

- [ ] 6.1 Remove hand lists per level after soak; keep `deny`. Tests: n/a. Ledger: config diff in rollout note.
- [ ] 6.2 Docs: `node-py/docs/handler-assignment.md` (modes, reasons, runbook), update `worker-release-rollout.md`, `compilation-authorization.md` (host verifier), `_plans/durable-workloads.md` stale lines.
- [ ] 6.3 Benchday companion change: bundle builder emits v2 manifests and a profile fragment; shrink `docs/harmony-worker-enrolment.md`; package remaining on-disk bundles (flutter-check, image, e2e.dependencies/full) as releases. Owned in the Benchday repo.
- [ ] 6.4 `openspec validate --specs` and `--changes` green; archive when phase 3 is live.
