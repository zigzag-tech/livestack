# Tasks - typed-outcome-causes-and-blockers

Work in `~/worktrees/livestack/work-ownership-and-causes`. Tests run with
`~/livestack/node-py/.venv/bin/python -m pytest` under `nice`. Each task names its tests and its ledger obligation.
Nothing here is implemented yet; this change is a proposal awaiting owner confirmation.

## 1. Vocabulary and storage

- [ ] 1.1 `model.py`: cause enum, `retry` advice, validators (coerce unknown kinds, bound evidence), `Limits` for the new opt-in
  deadlines. Tests: `tests/test_outcome_causes.py` (validator, coercion, size bounds). Ledger: none.
- [ ] 1.2 `schema.sql` + guarded migration: `jobs.cause`, `jobs.placement`, `attempts.progress_changed`. Tests: migration on an
  old database file; old rows report `predates_causes`. Ledger: none.
- [ ] 1.3 `store.py`: `_terminal_result` requires a cause; `complete`, `_abandon`, `_expire`, `cancel` pass it; `_job` returns
  `cause` and `placement`; `_limit_breach` renders from the cause. Tests: every terminal path stores a cause (property test over
  the lifecycle: no terminal job has `cause is None` unless migrated). Ledger: `completion_record` gains `cause_kind`.

## 2. Worker evidence

- [ ] 2.1 `resource_usage.py`/`worker.py`: bounded sampling of `memory.events` and `pids.events` each turn; keep `last_events`.
  Tests: `tests/test_worker_cause_sampling.py` against a temp cgroup tree. Ledger: none.
- [ ] 2.2 `supervision.py` `inspect`: include `ExecMainStatus`; `worker.py` `classify_stop` with the precedence in design §2; the lease
  keeper's `WorkNotAlive` triggers the stop but no longer names the cause. Tests: fixtures for each rule incl. unreadable ->
  `unknown`; real-cgroup OOM test (design §8) with positive control. Ledger: worker log line per terminal attempt.
- [ ] 2.3 Darwin/Windows: map existing receipt fields to causes. Tests: existing `test_darwin_*`/`test_windows_*` suites extended. Ledger: none.

## 3. Placement blockers and opt-in deadlines

- [ ] 3.1 `placement.py`: `{code, detail}` reasons; `jobs.placement` written on change only. Tests: one per blocker code; steady wait performs
  no UPDATE. Positive control: old `placement.py` overwrites `reason` every round (assert the counter). Ledger: unchanged records.
- [ ] 3.2 `_expire`: `max_queue_seconds` and handler `progress_deadline_seconds`. Tests: fake clock; a handler declaring a deadline
  without progress reporting is refused at load. Ledger: `deadline_expired`/`stalled_no_progress`/`unplaceable` causes appear in the completion record.

## 4. Docs and deploy

- [ ] 4.1 `_plans/durable-workloads.md` (breach rule, causes table) and `HARMONY.md` (cause kinds, blockers).
- [ ] 4.2 Authority release first (causes appear for authority-derived kinds), then worker release per worker class; mixed fleets are valid
  (old workers produce `unknown`/`infrastructure` with `cause: null`-free coercion, never a rejected result).
