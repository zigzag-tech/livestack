# Tasks — host-memory-ledger

Work in `~/worktrees/livestack/host-memory-ledger` (branch `agent/host-memory-ledger`).
Tests run with `~/livestack/node-py/.venv/bin/python -m pytest` under `nice`. Each task
names its tests and its ledger obligation.

## 1. Measurement

- [x] 1.1 `livestack_node/hostview.py`: meminfo, PSI, swap-in rate, cgroup memory,
  attempt cgroup scan, learned service peaks persisted in the state dir.
  Tests: `tests/test_hostview.py`. Ledger: none.

## 2. Worker

- [x] 2.1 `worker.py` `report()`: optional `capacity` (measured host when absent), `host`
  block from `HostView`. Tests: `tests/test_hostview.py` (worker report against a temp
  tree), existing `tests/test_workload_worker.py`. Ledger: none.

## 3. Authority

- [x] 3.1 `store.py` `register`: accept and bound `host`. Tests:
  `tests/test_workload_host_memory.py` (malformed blocks refused). Ledger: none.
- [x] 3.2 `placement.py`: learned handler peak, claim-based memory on measured hosts,
  pressure gate. Tests: `tests/test_workload_host_memory.py`, existing
  `tests/test_workload_store.py`, `tests/test_placement_principal_cap.py`.
  Positive control: overcommit test fails on the old `placement.py`.
  Ledger: job `reason` names memory arithmetic / pressure (design §5).

## 4. Deploy (design §7)

- [x] 4.1 Authority release staged + repointed; verified.
- [x] 4.2 zz-joe workers: release + config (`host_services`, no `capacity`, default
  reserve); restarted idle; verified (design §8).
- [ ] 4.3 Remaining Linux workers. NOT DONE (2026-10-02): every other worker still runs
  its old release and is placed as before; see `_plans/durable-workloads.md`.
- [x] 4.4 Record the deploy and measurements in `_plans/durable-workloads.md`.
