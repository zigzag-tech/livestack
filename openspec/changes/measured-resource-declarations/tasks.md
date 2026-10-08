# Tasks - measured-resource-declarations

Work in `~/worktrees/livestack/measured-resources-storage-headroom`. Tests run with
`~/livestack/node-py/.venv/bin/python -m pytest` under `nice`. Every task names its
tests and its ledger obligation (a bounded event/record naming the decision).

## 1. Measurement

- [x] 1.1 Measure on a real kernel whether the attempt cgroup stays readable after the unit
  enters `failed` (design R1). Record the result in `node-py/docs/measured-resources.md`.
  Tests: a script-level control. Ledger: none.
- [x] 1.2 `resource_usage.py`: add `source`, workspace disk delta; sample helper shared by
  the exit receipt and the worker loop. Tests: positive controls for memory, cpu, disk.
- [x] 1.3 `worker.py` loop: keep the last sample, merge on no-receipt, classify OOM from the
  sample. Tests: SIGKILLed-wrapper control fails on `origin/main`. Ledger: attempt result
  carries `source` and `resource_evidence`.

## 2. Authority

- [x] 2.1 `store.py`: typed `resource_limit` cause (`memory`/`tasks`/`disk`), non-retryable.
  Tests: updated `test_workload_store.py`. Ledger: result cause.
- [x] 2.2 `resource_history.py` + table, bounded insert/trim in the finalise transaction,
  bounded backfill, one grouped read. Tests: statement count independent of handler
  count; bound holds under 10x inserts.
- [x] 2.3 Share `placement._learned_peak` with the history. Tests: existing
  `test_workload_host_memory.py` unchanged and green.
- [x] 2.4 Audit (`declared_below_observed`, `admit_below_typical`) in handler status and
  roster. Tests: synthetic history; below `min_samples` never fires.

## 3. Policy

- [x] 3.1 `config.py` `resource_floor` schema, SIGHUP reload, 422 refusal text. Tests: bad
  keys fail closed without echoing values; floor boundary at exactly `observed x margin`.
  Ledger: refusal event with evidence.
- [x] 3.2 `strict` / unreadable-history behaviour. Tests: both modes.

## 4. Metrics

- [x] 4.1 `metrics_schema.py`, validation on store, `metrics_undeclared_total`, manifest
  `metrics`. Tests: unknown name dropped and counted; sub-phase sum <= whole control.
- [x] 4.2 `node-py/docs/measured-resources.md` including the "measure the instrument"
  checklist (see design 7).

## 5. Spec and docs

- [x] 5.1 `openspec validate --specs` and `openspec validate measured-resource-declarations`.
- [x] 5.2 Update `_plans/durable-workloads.md` (stale statements named in the proposal).
- [ ] 5.3 Benchday follow-ups doc (separate repo task, see
  `benchday-followups.md` next to this file).

## 6. Rollout (after owner confirmation)

- [ ] 6.1 Authority release first, verify with `tools/check-authority-release.py`;
  then workers. Rollback = previous release; table is derived data.
