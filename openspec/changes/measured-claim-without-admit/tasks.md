- [x] 1. `placement._memory_claim` / `_learned_peak(declared, min_samples)`; scheduler demand uses the claim.
  Tests: `tests/test_workload_host_memory.py` (five new; the first fails on the old code).
- [ ] 2. Deploy through `tools/deploy-authority-release.sh`; verify the waiting hub job is claimed.
- [x] 3. Cores: `placement._cpu_claim` / `charged_need`, `resource_usage.CpuPeak` -> `cpu_peak_cores` -> `resource_history`;
  roster arithmetic shares `charged_need`. Tests: `tests/test_workload_host_memory.py` (cores section), `tests/test_workload_resource_history.py`.
- [ ] 4. Deploy (same release as 2); peaks accrue after the worker release reaches the workers.
