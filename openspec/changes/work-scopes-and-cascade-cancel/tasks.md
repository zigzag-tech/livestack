# Tasks - work-scopes-and-cascade-cancel

Work in `~/worktrees/livestack/work-ownership-and-causes`. Tests run with
`~/livestack/node-py/.venv/bin/python -m pytest` under `nice`. Each task names its tests and its ledger obligation.
Nothing here is implemented yet; this change is a proposal awaiting owner confirmation.

## 1. Schema and model

- [ ] 1.1 `schema.sql` + guarded migration: `scopes` table, `jobs.scope`, `jobs_scope` index. Tests: migration on a
  pre-change database file in `tests/test_work_scopes.py`. Ledger: none.
- [ ] 1.2 `model.py`: validate `scope` (`key` name pattern, `lease_seconds` range), `Limits.scope_*` with hard ceilings and
  `scope_required_handlers`; include `scope` in the idempotency hash. Tests: invalid shapes refused. Ledger: none.

## 2. Authority

- [ ] 2.1 `store.py`: refactor `cancel` body into a shared helper; add `close_scope`, `renew_scope`, `get_scope`; `submit`
  upserts/refuses; `_expire` closes expired scopes (LIMIT 16); `_prune` removes unreferenced closed scopes.
  Tests: `tests/test_work_scopes.py` (cascade of queued and running, cleanup hold on worker, replay refusal, lease expiry,
  capacity refusal, unknown-scope 404 vs empty-scope zeros). Ledger: `scope_close` record per close with count > 0.
- [ ] 2.2 `http.py` + `client.py`: three routes; `capabilities` reports `scopes`. Tests: HTTP round trip incl. principal
  isolation (another principal cannot close or read your scope). Ledger: none.

## 3. Invariants

- [ ] 3.1 `tests/test_work_scopes_model.py`: S1-S6 with seeded sequences. Positive control: with the cascade
  removed the test fails (assert this in a second test that monkeypatches the helper out). Ledger: none.

## 4. Docs and deploy

- [ ] 4.1 Update `_plans/durable-workloads.md` (job lifecycle section) and `HARMONY.md` operator reference.
- [ ] 4.2 Authority release staged and repointed per the existing authority-release runbook; verify with a scoped job
  closed from the client against the staging authority. No worker release needed.
