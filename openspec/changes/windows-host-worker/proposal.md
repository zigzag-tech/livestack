## Why

The workload worker runs on Linux (systemd user units, cgroups) and, since
`apple-host-compilation`, natively on macOS (launchd jobs). A Windows host can
only take part through a WSL guest, which cannot build anything that needs the
Windows toolchain itself: MSVC, the Windows SDK, `x86_64-pc-windows-msvc` Rust
targets, signing. Benchday's Windows daemon is therefore cross-built for
`x86_64-pc-windows-gnu` on a Linux host, and nothing Windows-native has an
admitted, bounded place to run.

Realises `_plans/durable-workloads.md` (installed handlers on enrolled hosts) for
a Windows host; that record still describes only the "Linux/WSL executor".

## What Changes

- A Windows worker executor: each attempt is a named Job Object (the systemd
  unit's role). The bounded wrapper is created suspended, assigned to the job
  and only then resumed, so every process of the attempt is a member from its
  first instruction; the kernel enforces the job's commit limit (`need.memory_bytes`),
  active-process limit (`max_tasks`) and hard CPU rate (`need.cpu`), and
  `TerminateJobObject` stops the whole tree. Stop returns only when the job has
  no process left. The job name derives from worker and attempt, so a
  restarted worker finds and stops it.
- The wrapper turns the kernel's limit notifications into the receipt fields the
  Linux cgroup fills (`oom_kill`, `pids_max_events`), enforces wall time, and
  reads the pipe on a thread (Windows `select()` takes sockets only).
- Supervision of the worker itself: a Windows service (`windows_service.py`,
  stdlib ctypes, no pywin32) run as a dedicated non-administrator account, with
  SCM failure actions restarting it; a dying worker thread fails the service so
  the SCM restarts it.
- Measurement: available memory from `GlobalMemoryStatusEx` (a WSL/Hyper-V VM is
  an ordinary consumer there), CPU load from `GetSystemTimes` deltas (no
  `loadavg`), disk from `GetDiskFreeSpaceEx`. The Windows worker does not
  publish the Linux `host` block; a WSL worker on the same machine is enrolled
  under the SAME physical `host`, so placement charges every admission on that
  machine against both.
- New compilation class `windows` (MSVC, Windows SDK, signing tools), granted
  per physical host by operator policy like every class.
- Portability fixes the worker needs on Windows: the journal lock
  (`msvcrt.locking`), no directory fsync, read-only FILES in workspace removal,
  no POSIX mode comparison in source unpacking, `TEMP`/`TMP` in the handler
  environment, `HARMONY_JOB_OBJECT` naming the attempt's job, and the enrollment
  probe reporting the job's kernel limits.
- Specified, not yet implemented: launch verification on Windows (design
  "Verifier"). Until it ships, a handler classified for compilation cannot
  launch on a Windows worker (`verify_launch` refuses the platform), so the
  first Windows handlers run unclassified, as `benchday.release.daemon.linux.v1`
  does today.

## Capabilities

### New Capabilities

- `windows-host-worker`: bounded, supervised workload execution on a Windows host.

### Modified Capabilities

None (`compilation-authorization` is still an unarchived change; its behaviour on
Linux and macOS is unchanged).

## Impact

New `workloads/windows_proc.py`, `windows_supervision.py`, `windows_service.py`;
`worker.py`, `bounded_exec.py`, `supervision.py`, `probe.py`, `archive.py`,
`input_cache.py`, `docker_runtime.py` (import order), `compilation_policy.py`
(CLASSES). Deploy order for the class: authority with the new class before any
policy names it (an old authority refuses a policy naming an unknown class,
which would refuse ALL compilation).
