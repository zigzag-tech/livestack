## Context

Linux containment is a systemd unit's cgroup: the kernel puts every descendant
in it, systemd kills it whole, and memory/CPU/task limits are enforced by the
kernel. macOS has none of that. What it has: launchd (a trusted process manager
that runs each job in its own process group and kills the group on removal),
`libproc` (process parent, group, start time and physical footprint of any
process the caller may inspect), and `LOCAL_PEERCRED`/`LOCAL_PEERPID` on Unix
sockets.

## Decisions

**Durable state and ids.** The authority owns jobs/attempts (unchanged). The
worker owns its journal and one launchd job per attempt, labelled
`io.livestack.harmony-work.<sha256(worker)[:16]>.<attempt>`; the label is the
deterministic identity a restarted worker uses to find and stop it (the systemd
unit name's role). launchd owns the job's PID; the verifier reads it from launchd
(`launchctl print gui/<uid>/<label>`), never from a file the handler could write.

**Domain.** `gui/<uid>`: Xcode, codesign and the login keychain need the user's
Aqua session. The worker itself is a LaunchAgent in that session.

**Limits.** launchd cannot cap memory or CPU on modern macOS, and RLIMIT_NPROC is
per user (capping it would break every other process of that user). So the
bounded wrapper (`bounded_exec.py`) enforces them from its own argv, which launchd
records: every 0.5 s it walks the process tree from its own PID plus its process
group, sums `ri_phys_footprint`, and on footprint > memory cap, tasks > cap or
wall time > max kills the tree and writes `oom_kill`/`pids_max_events` into the
exit receipt (the same fields the Linux receipt carries, so the worker classifies
them as infrastructure exactly as it does a cgroup OOM). CPU is not capped; the
job runs at `Nice` 10 so interactive tenants keep priority. This is weaker than a
cgroup: a spike shorter than one sample can overshoot, and the cap is enforced
by kill, not by reclaim. Stated, not hidden.

**Containment.** A process is inside the attempt iff walking its parent chain
reaches the launchd job's PID with the same start time. A process that
double-forks with `setsid` and is reparented to launchd escapes both ancestry and
the process group; the wrapper remembers every descendant it has seen (bounded,
4096 entries) and kills the survivors when it exits, and `stop()` kills the
current tree plus the group. A deliberately hostile handler can still escape —
this design constrains supervised build tools, as the Linux one constrains
unguarded tools only to its cgroup.

**Memory placement.** The macOS worker does not report the Linux `host` block.
Placement's measured-host path keys by physical host; on xc-mac-studio a Lima VM
worker shares the host, and charging the VM's e2e claims against macOS
reclaimable memory would double count the VM's already-allocated RAM and starve
it. Instead the worker clamps `available.memory_bytes` with the existing
`host_pressure` reading (published by `harmony-host-pressure` from `vm_stat`,
zero while the host swaps), so an Apple job is admitted only while the Mac really
has the room. The learned attempt figure is the tree's physical footprint.

**Verifier.** Same wire contract (version 1, 5 s, 16 KiB). Root LaunchDaemon;
config and registry under `/private/etc` and socket under `/private/var/livestack-compilation` (`/private/var/run` is group-writable by `daemon` on macOS, which the trust walk refuses)
(`/etc` and `/var` are symlinks on macOS and the trust walk refuses symlinks).
`machine_id` is `IOPlatformUUID` lowercased without dashes (32 hex, same shape
as Linux). Peer uid via `LOCAL_PEERCRED`, pid via `LOCAL_PEERPID`; identity is
(pid, start time) from `proc_pidinfo`, rechecked after the authority round trip.

**Class.** `apple` is one more fixed class. Every bound that is "at most
len(CLASSES)" grows by one; receipts stay per-class files.

## Risks

- Policy reload used to kill running compile attempts on ANY revision change
  (`compilation_policy_revision_changed` on renewal; four e2e attempts died to a
  pure widening on 2026-10-02). Renewal now revokes only when the host no longer
  grants every admitted class (narrowing, host removal, expiry) or the
  handler's classification changed; a widening or revision-only change keeps
  the attempt, and its receipt keeps the admitting revision as evidence. The
  `apple` grant is made only after this is live on the authority.
- An old authority reading a policy that names `apple` refuses all compilation;
  the authority must run this code before the policy names the class.
