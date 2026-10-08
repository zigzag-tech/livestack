# Tasks - storage-headroom-admission

Work in `~/worktrees/livestack/measured-resources-storage-headroom`. Tests run with
`~/livestack/node-py/.venv/bin/python -m pytest` under `nice`. Each task names its tests
and its ledger obligation. Do not edit `authority.json` retention values or Benchday's
`docs/daemon-storage-bounds.md` (concurrent agent).

## 1. Effective bounds

- [x] 1.1 `storage_bounds.py` + `config.py` section, `statvfs` seam, log/status record.
  Tests: boundary +-1 byte (fails on `origin/main`), statvfs failure, bad config fails
  closed. Ledger: bound record logged at startup/reload/change.
- [x] 1.2 `blobs.py` put/put_range/upload-grant consult it; HTTP 507 named refusal.
  Tests: control above. Ledger: refusal event with figures.
- [x] 1.3 Proactive bounded GC with once-per-window guard. Tests: GC counter, referenced-
  only store, deletion never touches referenced blobs.

## 2. Retention tiers

- [x] 2.1 `retention_tiers` schema; tiered job/attempt expiry; compatible reference rules
  with `ttl_seconds` + `keep_newest`; `retain` exemption. Tests: straddling ages; release
  prefix rule; "never" requires acknowledgement.
- [x] 2.2 `unbounded_references` status and `GET /retention/plan` dry run. Tests: unmatched
  owner shown; plan deletes nothing.

## 3. Worker and placement

- [x] 3.1 `disk_unavailable` in the report; roster `reserve_exceeds_free`; startup warning.
  Tests: reserve 64 GiB over free 60 GiB.
- [x] 3.2 Placement `reason_code` + figures; stall event after `stall_report_seconds`.
  Tests: existing placement tests green; stager scenario reproduced.

## 4. CPU signal

- [x] 4.1 `cpu_admission.py`: `psi_some`, `runqueue`, schema (plain Python, no pydantic in
  the worker). Tests: fake `/proc`.
- [x] 4.2 Self-test with synthetic burn; `inert` and `active_unverified` states. Tests: real
  burn moves signal and not `full`; frozen fake reports `inert`, no silent fallback.
- [x] 4.3 Update `node-py/docs/worker-cpu-admission.md`.

## 5. Status, docs, validation

- [x] 5.1 Status and roster surfaces (free space, state, effective bound, refusals).
- [x] 5.2 `_plans/durable-workloads.md` hard-bounds table updated.
- [x] 5.3 `openspec validate --specs` and `openspec validate storage-headroom-admission`.

## 6. Rollout (after owner confirmation)

- [ ] 6.1 Authority release first (`tools/check-authority-release.py`), config sections
  added in a separate reviewed commit with `/retention/plan` output attached; workers next.
