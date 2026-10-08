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

Rollout record 2026-10-08: authority release livestack-8923e025 deployed (backup release-20261008T230506Z; a first
attempt with 80cf377b rolled back automatically because the live config already used the newer `rollout` role).
Config added to authority.json (backup authority.json.bak-measured-storage-sections-20261008T230541Z):
`storage_bounds` (capacity 0.5, 40 GiB floor; the 10% fraction was NOT applied: ~183 GiB floor > ~130 GiB free
would refuse all uploads) and `retention_tiers` (succeeded 3 d, failed 14 d, release refs keep 10 / 14 d).
Workers: only xc-tower-attune-1 rolled (canary). OPEN: roll remaining workers (they classify OOM without a
receipt and report disk_unavailable/cpu_signal only after their release moves); then archive.
