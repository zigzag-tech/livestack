# Measured resources

How an attempt's resource use is measured, what survives a kill, and how to check the
instrument itself. Design: `openspec/changes/measured-resource-declarations/`.

## What survives an OOM kill (measured 2026-10-08)

`node-py/scripts/probe-unit-cgroup-after-oom.sh direct|child` launches a transient
`--user` unit shaped like `supervision.py` (`Type=exec`, `OOMPolicy=kill`, `MemoryMax=64M`,
`MemorySwapMax=0`, `KillMode=control-group`) that allocates 256 MiB, then polls.

Result on systemd 259.5, kernel 7.0.0-38, cgroup2, both shapes (main process allocates;
wrapper spawns an allocating child):

| What | After `ActiveState=failed` (about 200 ms after the kill) |
|---|---|
| cgroup directory (`memory.events`, `memory.peak`, `pids.*`) | **gone** (never readable at the flip) |
| `Result` | `oom-kill` |
| `OOMKills` | `1` |
| `MemoryPeak` | `67108864` (the limit) |
| `CPUUsageNSec` | retained |
| `TasksCurrent`, `IOReadBytes`, `ControlGroup` | `[not set]` / empty |

Consequences: the OOM verdict comes from the retained unit properties, not a late
cgroup read; tasks and disk evidence come only from the last worker-loop sample. Re-run
the probe on any new systemd major version or on a new OS before relying on this.
