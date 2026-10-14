# Tasks - typed-outcome-causes-and-blockers

Work in `~/worktrees/livestack/work-ownership-and-causes`. Tests run with
`~/livestack/node-py/.venv/bin/python -m pytest` under `nice`. Each task names its tests and its ledger obligation.
Owner confirmed 2026-10-08 (fixed cause list with retry advice and cgroup/systemd evidence; Livestack-side stall and queue-wait deadlines opt-in).

## 1. Vocabulary and storage

- [x] 1.1 `model.py`: cause enum, `retry` advice, validators (coerce unknown kinds, bound evidence), `Limits` for the new opt-in
  deadlines. Tests: `tests/test_outcome_causes.py` (validator, coercion, size bounds). Ledger: none.
- [x] 1.2 `schema.sql` + guarded migration: `jobs.cause`, `jobs.placement`, `attempts.progress_changed`. Tests: migration on an
  old database file; old rows report `predates_causes`. Ledger: none.
- [x] 1.3 `store.py`: `_terminal_result` requires a cause; `complete`, `_abandon`, `_expire`, `cancel` pass it; `_job` returns
  `cause` and `placement`; `_limit_breach` renders from the cause. Tests: every terminal path stores a cause (property test over
  the lifecycle: no terminal job has `cause is None` unless migrated). Ledger: `completion_record` gains `cause_kind`.

## 2. Worker evidence

- [x] 2.1 (already shipped by measured-resource-declarations: the loop samples memory.events/pids.events each turn into `resource_sample`) `resource_usage.py`/`worker.py`: bounded sampling of `memory.events` and `pids.events` each turn; keep `last_events`.
  Tests: `tests/test_worker_cause_sampling.py` against a temp cgroup tree. Ledger: none.
- [ ] 2.2 (PARTLY: `supervision.inspect` now asks ExecMainStatus and `unit_evidence` reports `unit_result`/`exec_main_status`; committed but reaches the authority only after a worker release. The authority classifies without it, so `classify_stop` lives in the authority `causes.derive`, not the worker; the `WorkNotAlive` demotion is moot because the exception path already merges unit evidence.) `supervision.py` `inspect`: include `ExecMainStatus`; `worker.py` `classify_stop` with the precedence in design §2; the lease
  keeper's `WorkNotAlive` triggers the stop but no longer names the cause. Tests: fixtures for each rule incl. unreadable ->
  `unknown`; real-cgroup OOM test (design §8) with positive control. Ledger: worker log line per terminal attempt.
- [x] 2.3 (no change needed: receipts already carry oom_kill, which the authority maps) Darwin/Windows: map existing receipt fields to causes. Tests: existing `test_darwin_*`/`test_windows_*` suites extended. Ledger: none.

## 3. Placement blockers and opt-in deadlines

- [x] 3.1 `placement.py`: `{code, detail}` reasons; `jobs.placement` written on change only. Tests: one per blocker code; steady wait performs
  no UPDATE. Positive control: old `placement.py` overwrites `reason` every round (assert the counter). Ledger: unchanged records.
- [x] 3.2 `_expire`: `max_queue_seconds` and handler `progress_deadline_seconds`. Tests: fake clock; a handler declaring a deadline
  without progress reporting is refused at load. Ledger: `deadline_expired`/`stalled_no_progress`/`unplaceable` causes appear in the completion record.

## 4. Docs and deploy

- [x] 4.1 `_plans/durable-workloads.md` (breach rule, causes table) and `HARMONY.md` (cause kinds, blockers).
- [ ] 4.2 (authority half DONE, livestack a3a6a47f deployed 2026-10-08; worker release for `unit_result` evidence pending, any worker roll after 8923e025 carries it) Authority release first (causes appear for authority-derived kinds), then worker release per worker class; mixed fleets are valid
  (old workers produce `unknown`/`infrastructure` with `cause: null`-free coercion, never a rejected result).

## Record (2026-10-08)

Authority deployed with `work-scopes-and-cascade-cancel` (release `livestack-a3a6a47f`, see that change's record). Deviations: causes are derived in the authority
(`workloads/causes.py`) from the completion rather than in a worker `classify_stop`, so mixed fleets work with no worker release; `progress_deadline_seconds`
and `max_queue_seconds` are both submission fields (schema 4), not a handler declaration; extra blocker codes `environment_not_found`,
`environment_profile_not_installed`, `disk_reserve`, `disk_insufficient`, `resources_insufficient`. `no_workers` cannot occur through `claim` (the claimer is itself a
fresh worker) and is kept for completeness. Tests: `tests/test_outcome_causes.py` (derivation table, every terminal path, seeded lifecycle property with a
mutant, steady wait performs no UPDATE with a positive control, recorded systemd properties) and the real-kernel OOM test in `tests/test_workload_worker.py`.
Remaining before archive: the worker release (2.2/4.2).

## Record (2026-10-12)

Worker half verified on origin/main (`resource_usage.py` reports `unit_result`/`exec_main_status`; `causes.py` consumes them). No separate
worker change remains: the next worker release built from main carries it, so it is batched with the pending cores-claim worker roll
(`fd17f036`), one worker at a idle gap. Until a worker runs it, a unit killed by systemd shows `cause=unknown` without `unit_result`.
ZZOPS now prints `job.cause` (kind, retry, evidence) in the Harmony terminal failure text, or `cause: none recorded` (zzops 772e371).

## Record (2026-10-14)

`handler_failed` evidence was only `exit_code`, and `reason` stayed empty, so a job that failed on a landed SyntaxError read "failed,
authority gave no reason" in ZZOPS. The worker now ships `result.log_excerpt` (`workloads/log_excerpt.py`, from command.log) and the
authority carries it as `cause.evidence.excerpt` and derives `reason` from it (`causes.reason_of`). Needs a worker roll to produce the
excerpt (old workers: cause as before, no excerpt) and an authority deploy to carry it. Tests: `tests/test_log_excerpt.py`, and the
real systemd + node process test `test_handler_running_a_script_with_a_syntax_error_ends_with_a_labelled_cause` in
`tests/test_workload_worker.py`. Pre-existing failure unrelated to this: `test_outcome_causes.py::test_busy_worker_blocker_and_cleared_on_claim`.
