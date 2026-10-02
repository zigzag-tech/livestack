# Tasks — learned-gpu-footprint

Worktree `~/worktrees/livestack/learned-gpu-footprint`, branch
`agent/learned-gpu-footprint`. Tests run on zz-joe (the local venv has no `shared_py`)
with `~/harmony-embed/venv/bin/python -m pytest` under `nice`.

## 1. Measurement

- [x] 1.1 `measure.py`: `record_resident`, reserved-aware `end`, `measure_load`,
  bounded store with quarantine and `store_error`. `manager.py`: measure real loads.
  Tests: `tests/test_learned_gpu_footprint.py` (real HTTP, real store files),
  `tests/test_measure.py`. Positive control: all 10 new tests fail on `e8d76d70`.
  Ledger: none.

## 2. Report and plan

- [x] 2.1 `facade.py` `/residence`: learned footprint, `footprint_source`, `learned`.
  `planner.py`: `"allocator"` in `MEASURED_SOURCES`. Tests:
  `test_broker_plans_with_the_measured_footprint` (RestPeer over HTTP + `_World`).
  Ledger: existing decision text names the source.

## 3. klein in version control

- [x] 3.1 `livestack_node/imagegen/klein.py` from zz-joe's `klein_worker.py` (encoder
  GPU-resident). Tests: `tests/test_imagegen_klein.py`. Ledger: none.

## 4. Deploy (design §7)

- [ ] 4.1 zz-joe release + drop-ins, idle restart, prior back to 3e9, one generation,
  verify node and broker report `allocator`. Record in `examples/harmony-image/README.md`.
