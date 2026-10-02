## Why

Compilation admission (`compilation-launch-authorization`) is Linux-only: the
worker supervises attempts as `systemd-run --user` units, the launch verifier
proves containment from `/proc/<pid>/cgroup`, and `verify_launch` refuses every
platform but Linux. Apple builds (macOS/iOS apps, DMG, Xcode, CocoaPods, Apple
Rust targets) can only run on a macOS host, so under hard admission they have no
admitted place to run at all. Today Benchday runs them over ssh on xc-mac-studio,
outside Harmony, with no reservation, class or memory bound on a Mac that
already swaps (benchday `docs/mac-memory-budget.md`).

Realises `_plans/durable-workloads.md` (installed handlers on enrolled hosts) for
a macOS host; that record still says workers are "Linux/WSL" only.

## What Changes

- New compilation class `apple` (Xcode SDK, Apple linkers/signing tools,
  CocoaPods). Operator policy grants it per physical host like every class.
- A macOS worker executor: each attempt is a per-attempt launchd job in the
  worker user's GUI domain (the keychain and Xcode need it), supervised by the
  existing bounded wrapper, which on macOS also enforces the attempt's memory
  (physical footprint of the process tree), task count and wall time, because
  macOS has no cgroups. Stop kills the whole tree and process group and verifies
  nothing of it survives before capacity is released.
- The worker on macOS reports measured memory through the existing
  `host_pressure` clamp and does NOT publish a Linux `host` block, so a Lima VM
  worker on the same physical host keeps its own placement semantics.
- Launch verification on macOS: the root verifier authenticates peers with
  `LOCAL_PEERCRED`/`LOCAL_PEERPID`, proves containment by process ancestry from
  the launchd job's own PID (read from launchd, not from worker files), binds the
  machine by `IOPlatformUUID`, and checks the attempt's enforced limits from the
  arguments launchd holds for the job.
- Attempt memory learning uses the tree's physical footprint as the
  non-reclaimable figure on macOS.

## Capabilities

### New Capabilities

- `darwin-host-compilation`: admitted, bounded compilation on a macOS host.

### Modified Capabilities

None (the `compilation-authorization` capability is still an unarchived change;
its Linux behaviour is unchanged).

## Impact

`workloads/compilation_policy.py` (CLASSES), `launch_contract.py`,
`launch_verifier.py`, `worker.py`, `bounded_exec.py`, new `darwin_proc.py` and
`darwin_supervision.py`. Deploy order: authority with the new class first (an old
authority refuses a policy naming an unknown class, which would refuse ALL
compilation), then the policy grant, then the macOS worker and verifier.
