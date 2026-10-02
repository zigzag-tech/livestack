## Context

Linux containment is a systemd unit's cgroup; macOS has none and uses a launchd
job plus wrapper-enforced limits. Windows has a kernel object made for this: the
Job Object. A process in a job cannot leave it unless the job grants breakaway
(ours never does); its children are created inside it; the kernel enforces a
job-wide commit limit, active-process limit and CPU rate cap; one call
terminates every member; and `IsProcessInJob` answers membership for any
process. That is closer to a cgroup than launchd is.

## Decisions

**Durable state and ids.** The authority owns jobs/attempts (unchanged). The
worker owns its journal (`state_dir/active.json`) and one Job Object per attempt
named `Global\livestack-harmony-work-<sha256(worker)[:16]>-<attempt>`, the
deterministic identity a restarted worker uses (the systemd unit name's role).
`Global\` because the worker runs as a service (session 0) and an operator
inspects from another session; creating it needs `SeCreateGlobalPrivilege`,
which services and administrators have. The handler learns the name from
`HARMONY_JOB_OBJECT` and proves membership with `IsProcessInJob`, never trusting
the variable alone.

**Lifetime of the job.** The worker creates the job with the attempt's limits
and `KILL_ON_JOB_CLOSE`, creates the wrapper `CREATE_SUSPENDED` (and
`CREATE_BREAKAWAY_FROM_JOB` from the worker's own job when it has one, falling
back to nesting), assigns it, verifies membership, then resumes its threads: no
instruction of the attempt runs outside the job. The wrapper opens the job by
name and holds that handle for its life, so the job (and its name) survives a
worker restart; the worker holds its own handle while it supervises. When every
handle closes, kill-on-close ends any member left, so neither a dead worker nor
a dead wrapper can leave an attempt running unowned.

**Limits.** `need.memory_bytes` is the job's commit limit (private bytes, which
page cache never counts against: the non-reclaimable figure Linux learns from
`memory.stat`). On breach the allocation fails; the wrapper receives
`JOB_OBJECT_MSG_JOB_MEMORY_LIMIT` on the job's completion port, kills every
other member (cgroup `OOMPolicy=kill`) and writes `oom_kill: 1`. `max_tasks` is
the active-process limit; a refused spawn posts
`JOB_OBJECT_MSG_ACTIVE_PROCESS_LIMIT` and the receipt records
`pids_max_events: 1` (as Linux records a refused fork; nothing is killed).
`need.cpu` is a hard CPU rate cap in the job's unit (1/100 % of the whole
machine, so `cpu / cpu_count * 10000`, rounded: the quota is the nearest step).
Wall time is enforced by the wrapper. Members run at below-normal priority and
`DIE_ON_UNHANDLED_EXCEPTION` keeps an error dialog from parking a dead process.
The kernel charges commit in its own granules: a peak may pass the limit by
under a MiB (measured 0.75 MiB on 256 MiB); it never runs away.

**Venv launchers.** A venv's `python.exe` is a launcher that runs the base
interpreter as its child in its own nested job. The wrapper therefore spares
its parent when it kills "everyone else", and a handler must find the attempt's
job by name, not as "the current job" (the innermost job may be the launcher's).
Installed handlers and the service should name the base interpreter.

**Supervision of the worker.** A Windows service (the systemd user unit's
role), implemented with advapi32 through ctypes: `StartServiceCtrlDispatcherW`
on the main thread, the worker loop on a thread, a stop control reports
`SERVICE_STOPPED`. A worker thread that dies ends the process WITHOUT reporting
stopped; the SCM counts that as a failure and applies the configured restart
actions (`sc failure ... actions= restart/5000/...`). The service runs as a
dedicated local account without administrator rights (attempts inherit its
token; the verifier below depends on the worker not being an administrator),
with `SeServiceLogonRight`. `PYTHONPATH` naming the immutable release travels in
the service's registry `Environment` value, as `Environment=` does in a unit.
A stop does not end attempts (each runs in its own job, held by its wrapper);
the next start reconciles them, as a systemd restart does.

**Workspace and permissions.** The workspace is a dedicated bounded volume: a
fixed-maximum VHDX formatted NTFS and mounted at a folder. The existing
dedicated-filesystem check holds unchanged (`st_dev` is the volume serial, which
differs from the parent's). Only SYSTEM, Administrators and the worker account
have access to it, to the state directory and to the config (which holds the
token). Releases and handler bundles are owned by Administrators and readable by
the worker account. Removal makes read-only FILES writable too (Windows refuses
to unlink a read-only file whatever its directory allows); source unpacking does
not compare POSIX modes, which Windows cannot represent (the manifest still
carries them).

**Memory placement on a shared machine.** A Windows host commonly carries a WSL
guest worker. Both principals name the same physical `host`, so placement's
existing per-host arithmetic applies: every attempt's admission on that host is
subtracted from both workers' free figures, and each must also fit its own
worker's observation. The Windows worker reports `GlobalMemoryStatusEx`
available memory, in which the WSL VM (`vmmem`) is already a consumer, and does
NOT publish the Linux `host` block: two different machines' views (guest and
host) under one `host` key would make placement's "freshest block wins" flap
between them. The cost is conservatism: a WSL attempt's realised memory is both
inside `vmmem` (so out of Windows' available) and still subtracted as an
admission. Without WSL memory reclaim the guest's page cache stays in `vmmem`
indefinitely and starves the Windows worker; hosts should set
`[experimental] autoMemoryReclaim` (operational, not code).

**Verifier (specified; implementation is task 4).** The root verifier's Windows
equivalent is a LocalSystem service listening on a named pipe whose DACL admits
the worker account. It identifies the peer with `GetNamedPipeClientProcessId`
and opens it with `PROCESS_QUERY_LIMITED_INFORMATION`; containment is
`IsProcessInJob(peer, job)` for the attempt's job, opened by name (the name is
derived from the configured worker and the attempt, never read from the peer);
the limits it checks against the authority receipt are the kernel's
(`QueryInformationJobObject`), not a file; the machine binding is the
`MachineGuid` registry value. Config and registry live under a directory only
SYSTEM and Administrators can write (`%ProgramData%\livestack`, ACL-checked the
way `trusted_json` checks root ownership). Same wire contract (version 1, 5 s,
16 KiB). Limitation, stated as on Linux: the worker account owns the job object,
so a hostile handler of that account could open it and widen its limits; the
design constrains supervised build tools, not hostile code.

**Class.** `windows` is one more fixed class (MSVC, Windows SDK, signing).
Policy grants are per physical host, so a WSL guest worker on a granted host
shares the grant; it does not advertise Windows handlers, and handlers are
operator-installed.

## Risks

- Policy reload kills running compile attempts when the revision changes. The
  grant of `windows` is a policy edit done in a window agreed with the policy
  owner, and only after the verifier ships.
- An old authority reading a policy that names `windows` refuses all
  compilation: the authority must run this code first.
- The CPU cap's granularity is 1/100 % of the machine (0.16 % of a core on 16
  CPUs); the probe reports the effective quota.
