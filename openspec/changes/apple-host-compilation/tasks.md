# Tasks

Ledger obligation: none of these decisions is a placement/routing decision of
the inference planners; workload placement already records its refusal reason
on the job (`jobs.reason`), which these tasks keep.

## 1. Contract

- [x] 1.1 Add `apple` to `CLASSES`; tests: `test_workload_compilation_policy.py::test_apple_class_granted_only_by_policy` (real HTTP/SQLite authority, Linux host advertising an apple handler is refused, macOS host admitted).

## 2. macOS supervision

- [x] 2.1 `darwin_proc.py` (libproc via ctypes: bsdinfo, children, footprint, peer credentials, platform UUID); tests: `test_workload_darwin.py` real processes on macOS.
- [x] 2.2 `LaunchdExecutor` with the SystemdExecutor interface; `bounded_exec.py` enforces memory/tasks/wall time on macOS; tests: real launchd jobs on macOS — grandchild stop, memory breach receipt, restart reconcile by label.
- [x] 2.3 Worker selects the executor by platform, samples attempt memory from the executor, omits the Linux `host` block on macOS; tests: worker report on macOS, existing Linux worker tests unchanged.

## 3. macOS launch verification

- [x] 3.1 Verifier and client accept macOS peers (LOCAL_PEERCRED/PEERPID, ancestry containment, launchd-held limits, IOPlatformUUID); tests: real root verifier on macOS — positive Apple-toolchain launch, copied metadata outside the job refused, wrong limits refused.

## 4. Deployment (operational evidence in `_plans/durable-workloads.md`)

- [ ] 4.1 Authority runs the new class before policy names it; policy grants `apple` to xc-mac-studio only, in a window agreed with the policy owner.
- [ ] 4.2 xc-mac-studio host worker (LaunchAgent, bounded APFS workspace volume) and root verifier (LaunchDaemon) installed; a real Apple build admitted there and the same handler refused on another host.

## Evidence (2026-10-02)

- xc-mac-studio (macOS 26.3, Xcode 26.4.1), `tests/test_workload_darwin.py`: 10 passed in 8.8 s —
  launchd grandchild + reparented group member stopped, memory breach (600 MiB in a 256 MiB cap)
  killed with `oom_kill` 1, task cap (`pids_max_events` 1), restarted worker finds the job by label;
  real root verifier on `/usr/bin/python3`: admitted `xcrun clang` Mach-O build under class `apple`,
  copied metadata outside the job refused (`compilation_peer_outside_attempt`), memory/CPU limit
  mismatch refused, unreserved class refused, wrong-machine config refuses to start.
- zz-joe (Linux): policy, full launch verifier, worker, supervision, host memory, service suites:
  116 passed, 10 skipped (the macOS suite), 164 s. Includes the new
  `test_apple_class_granted_only_by_policy`.
