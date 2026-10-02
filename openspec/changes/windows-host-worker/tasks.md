# Tasks

Ledger obligation: none of these decisions is a placement/routing decision of
the inference planners; workload placement already records its refusal reason
on the job (`jobs.reason`), which these tasks keep.

## 1. Contract

- [x] 1.1 Add `windows` to `CLASSES`; tests: `test_workload_compilation_policy.py::test_windows_class_granted_only_by_policy` (real HTTP/SQLite authority; a Linux host advertising a windows handler is refused by policy, the Windows host is placed with classes `rust`,`windows`).

## 2. Windows supervision

- [x] 2.1 `windows_proc.py` (kernel32 via ctypes: Job Objects, limits, accounting, completion-port notifications, memory status, system times, suspended-process resume); tests: `test_workload_windows.py` with real jobs on Windows.
- [x] 2.2 `JobObjectExecutor` with the SystemdExecutor interface; `bounded_exec.py` Windows path (job notifications, wall time, threaded pipe reader); tests: real jobs — detached grandchild stopped, kernel holds the limits, memory breach `oom_kill` 1, task cap `pids_max_events` 1, wall time, restarted worker finds the job by name, receipt and log.
- [x] 2.3 Worker on Windows: executor by platform, journal lock, memory/CPU/disk measurement, no `host` block, `TEMP`/`TMP`/`HARMONY_JOB_OBJECT`, read-only file removal, unpack without POSIX modes, probe reports job limits; tests: end-to-end job and `harmony.probe.v1` through a real authority on Windows; Linux suites unchanged.
- [x] 2.4 `windows_service.py` (SCM service via ctypes); tests: real service created with `sc.exe` — runs and stops; a dying worker is restarted by the SCM failure action.

## 3. Deployment (operational evidence below)

- [ ] 3.1 xc-win-1: dedicated worker account, bounded VHDX workspace, service, release; principal `xc-win-1-native` on host `xc-win-1`; a real `harmony.probe.v1` placed there.
- [ ] 3.2 WSL memory reclaim on xc-win-1 (`autoMemoryReclaim`), done only with `xc-win-1-wsl` drained.

## 4. Windows launch verification

- [ ] 4.1 Verifier service (LocalSystem, named pipe, `GetNamedPipeClientProcessId` + `IsProcessInJob`, kernel-held limits, `MachineGuid`) and `verify_launch` client on Windows; tests: real verifier on Windows — admitted MSVC launch, peer outside the job refused, limit mismatch refused.
- [ ] 4.2 Authority runs the `windows` class before policy names it; policy grants `windows` to xc-win-1 in a window agreed with the policy owner.
