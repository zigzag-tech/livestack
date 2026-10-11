- [x] 1. `placement._memory_claim` / `_learned_peak(declared, min_samples)`; scheduler demand uses the claim.
  Tests: `tests/test_workload_host_memory.py` (five new; the first fails on the old code).
- [ ] 2. Deploy through `tools/deploy-authority-release.sh`; verify the waiting hub job is claimed.
