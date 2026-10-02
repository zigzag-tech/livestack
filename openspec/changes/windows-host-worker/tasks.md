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

- [x] 3.1 xc-win-1: dedicated worker account, bounded VHDX workspace, service, release; principal `xc-win-1-native` on host `xc-win-1`; a real `harmony.probe.v1` placed there.
- [x] 3.2 WSL memory reclaim on xc-win-1 (`autoMemoryReclaim`), done only with `xc-win-1-wsl` drained.

## 4. Windows launch verification

- [ ] 4.1 Verifier service (LocalSystem, named pipe, `GetNamedPipeClientProcessId` + `IsProcessInJob`, kernel-held limits, `MachineGuid`) and `verify_launch` client on Windows; tests: real verifier on Windows — admitted MSVC launch, peer outside the job refused, limit mismatch refused.
- [ ] 4.2 Authority runs the `windows` class before policy names it; policy grants `windows` to xc-win-1 in a window agreed with the policy owner.

## Deployment record (2026-10-02)

- xc-win-1 (Windows 11 Pro 26200): CPython 3.12.10 machine-wide (winget); release
  `C:\harmony\releases\livestack-dba3fc37` (merge `dba3fc37`); service
  `LivestackWorkloadWorker` as `.\harmony-worker` via `node-py/deploy/windows/install-worker.ps1`;
  workspace VHDX 100 GB at `C:\harmony\work\xc-win-1-native`.
- Principal `xc-win-1-native` (host `xc-win-1`, claim_enabled) added by SIGHUP reload 08:0x UTC;
  worker registered `ready=1`, handlers `harmony.probe.v1`, no `host` block.
- Proof: job `99f17d4b72bf43eca26f9bc3fd924b6f` (`harmony.probe.v1`, selector `os=windows`)
  succeeded on attempt `05c0900f303d4bfc80933bf7ef9fdf36`: `isolation windows-job-object`,
  `memory_max 268435456`, `cpu_max 100000 100000`, `tasks_max 512`, peak commit 21 MiB. While a
  WSL e2e attempt held its 4 GiB admission and vmmem held its guest cache, the job waited as
  `insufficient shared host resources` on the native worker: the shared-host arithmetic.
- WSL reclaim: `xc-win-1-wsl` drained 08:10:20-08:21:47 UTC; `.wslconfig` gained
  `[experimental] autoMemoryReclaim=gradual`. Windows available memory 3995 MB (vmmem 15287 MB,
  e2e running) / 6574 MB (vmmem 12181 MB, idle) before; 14811 MB (vmmem 4828 MB) after restart.
  WSL generates no `.swap` units from fstab: the guest's swap files did not return after
  `wsl --shutdown` until a oneshot `wsl-fstab-swap.service` (`swapon -a`) was added; verified
  over a second restart.
