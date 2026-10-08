# Tasks - work-scopes-and-cascade-cancel

Work in `~/worktrees/livestack/work-ownership-and-causes`. Tests run with
`~/livestack/node-py/.venv/bin/python -m pytest` under `nice`. Each task names its tests and its ledger obligation.
Owner confirmed 2026-10-08 (lease default 30 min bounded 5..240 renewed by ZZOPS heartbeat; replay into a closed scope is 409 scope_closed; opt-in requirement).

## 1. Schema and model

- [x] 1.1 `schema.sql` + guarded migration: `scopes` table, `jobs.scope`, `jobs_scope` index. Tests: migration on a
  pre-change database file in `tests/test_work_scopes.py`. Ledger: none.
- [x] 1.2 `model.py`: validate `scope` (`key` name pattern, `lease_seconds` range), `Limits.scope_*` with hard ceilings and
  `scope_required_handlers`; include `scope` in the idempotency hash. Tests: invalid shapes refused. Ledger: none.

## 2. Authority

- [x] 2.1 `store.py`: refactor `cancel` body into a shared helper; add `close_scope`, `renew_scope`, `get_scope`; `submit`
  upserts/refuses; `_expire` closes expired scopes (LIMIT 16); `_prune` removes unreferenced closed scopes.
  Tests: `tests/test_work_scopes.py` (cascade of queued and running, cleanup hold on worker, replay refusal, lease expiry,
  capacity refusal, unknown-scope 404 vs empty-scope zeros). Ledger: `scope_close` record per close with count > 0.
- [x] 2.2 `http.py` + `client.py`: three routes; `capabilities` reports `scopes`. Tests: HTTP round trip incl. principal
  isolation (another principal cannot close or read your scope). Ledger: none.

## 3. Invariants

- [x] 3.1 `tests/test_work_scopes_model.py`: S1-S6 with seeded sequences. Positive control: with the cascade
  removed the test fails (assert this in a second test that monkeypatches the helper out). Ledger: none.

## 4. Docs and deploy

- [x] 4.1 Update `_plans/durable-workloads.md` (job lifecycle section) and `HARMONY.md` operator reference.
- [x] 4.2 Authority release staged and repointed per the existing authority-release runbook; verify with a scoped job
  closed from the client against the staging authority. No worker release needed.

## Record (2026-10-08)

Implemented in livestack `a3a6a47f` (main). Deviations from the design: the lease is mandatory with a default (owner decision), bounds
300..14400 s (`scope_lease_min_seconds`/`max`), so "no lease means no expiry" no longer applies; scope keys exclude `/` (path segment);
`scope_close` is a logged line (WARNING `scope_closed ...`) plus the scope row's `close_reason/closed_by/close_result`, NOT a decision-ledger record
(the ledger schema has no such decision kind; add one if "who killed my job" must outlive scope pruning); a refusal raised after an expiry cascade commits
the cascade first (`RefusedAfterCommit`). Tests: `tests/test_work_scopes.py` (real SQLite and real HTTP), `tests/test_work_scopes_model.py` (seeded S1-S6, 60 sequences by
default, `WORK_SCOPE_SEQUENCES=2000` for the full run; mutants without cascade and without the lease janitor are caught). Deployed to the live authority
(release `livestack-a3a6a47f`, backup `release-20261008T233219Z`), first run against a copy of the production database (migration of 588 jobs, scoped round trip),
then `tools/deploy-authority-release.sh` (check PASS, 22 of 22 workers ready after restart). Live verification: capabilities.scopes, scoped submit, renew, close (cascade
cancelled the job, cause `scope_closed`), replay 409. No worker release needed.
