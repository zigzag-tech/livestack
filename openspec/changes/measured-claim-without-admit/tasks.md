- [x] 1. `placement._memory_claim` / `_learned_peak(declared, min_samples)`; scheduler demand uses the claim.
  Tests: `tests/test_workload_host_memory.py` (five new; the first fails on the old code).
- [x] 2. Deployed through `tools/deploy-authority-release.sh` 2026-10-11 (hub release job 62c4da03 was claimed).
- [x] 3. Cores: `placement._cpu_claim` / `charged_need`, `resource_usage.CpuPeak` -> `cpu_peak_cores` -> `resource_history`;
  roster arithmetic shares `charged_need`. Tests: `tests/test_workload_host_memory.py` (cores section), `tests/test_workload_resource_history.py`.
- [x] 4a. Authority deployed 2026-10-11 23:09Z as `livestack-cpu-6e0dc516` (the live `livestack-claim-8af82266` tree with only
  placement/roster/resource_history/resource_usage/worker.py replaced); 22 of 22 workers ready, running attempts survived.
- [ ] 4b. Worker release (`worker.py`, `resource_usage.py`): until workers run it no `cpu_peak_cores` is recorded, so every cpu
  claim stays at `need`. Roll per node-py/docs/worker-release-rollout.md (drain, idle, restart).
