# Host health vs live capacity

Two facts about a machine, kept apart on purpose:

| | Capacity | Health |
| --- | --- | --- |
| What | RAM/VRAM/CPU the host has **now** | whether the host may take **new** work at all |
| Source | measured: the worker's `host` block (MemTotal, MemAvailable, PSI, tenants, `boot_id`), hostd's meters | an owner's verdict (e.g. a memory test) |
| Changes when | every heartbeat, every boot, every DIMM swap | only when the owner says so |
| Who writes it | nobody by hand; no inventory table | the owner (or a forced admin) |

**A capacity report never clears a health hold.** That is the rule this page exists for. It came
from node-h (2026-11-14 onward). Its non-ECC DIMMs flipped bit 60, the sticks were swapped one at a
time, and every swap changed the RAM it measured. A hold that a bigger report, a reboot or a 24 h
drain TTL could lift would put builds, E2E runs and model weights onto memory that corrupts them
silently.

## Workload authority (CPU work; what ZZOPS schedules through)

ZZOPS has no hardware table. It submits jobs with `need`/`admit` and the authority places them.
Both rules below are therefore enforced in `workloads/placement.py`, and ZZOPS needs no change.

### Live capacity

- A worker with no `capacity` in its config offers the measured machine. `capacity` is an optional
  operator ceiling, never a typed-in RAM size.
- Memory is charged from the freshest `host` block. A report older than `fresh_seconds` (60 s)
  offers nothing (`worker_stale`), so an offline host takes no new placements.
- `host.boot_id` (optional, from `/proc/sys/kernel/random/boot_id`): a report from an older boot
  than the host's newest is refused with `host_capacity_stale` and is never used as the host
  view. It is resubmitted on the worker's next report under the new boot. A report without
  `boot_id` (an older worker release, mid-roll) is not judged. **Roll the authority
  before workers.** An authority without this change rejects a `host` block carrying `boot_id` as
  `invalid host report`.
- A shrink stops new placements (`memory_insufficient`). Running attempts are never killed by a
  report.

### Durable host hold (`host_health` table)

```bash
python3 -m livestack_node.workloads.cli --config <caller/admin config> \
    host-hold node-h --owner memory-test --reason "bit-60 flips; verdict open"
python3 -m livestack_node.workloads.cli ... host-health
python3 -m livestack_node.workloads.cli ... host-release node-h --owner memory-test --reason "all-four soak clean"
```

The HTTP routes are `POST /v1/workloads/hosts/<host>/health/hold|release` and
`GET /v1/workloads/hosts[/<host>]/health`.

- Keyed by the principals' physical `host`, so every identity on the machine is withheld, including
  identities not registered yet.
- No expiry. `register` (any RAM, any boot), the claims tick and an authority restart leave it
  alone.
- Releasing as an owner other than the holder needs `force` (admin only). The `rollout` principal
  may hold but never release.
- Placement refuses with `host_health_held by <owner>: <reason>`. `claims.draining` includes the
  host's workers, so assignment, `handler_capacity` and the roster (`ineligible_reasons`) all see
  the hold.

## Harmony model placement (hostd + process-node facades)

`/etc/harmony/host-health.json` (override with `LIVESTACK_HOST_HEALTH_FILE`):

```json
{"state": "held", "owner": "memory-test", "reason": "bit-60 flips; verdict open"}
```

- If the file is absent, or says `"state": "eligible"`, the host is eligible. If it says `held`, or
  is **unreadable or malformed, the host is held** (fail closed).
- hostd reads it on every plan (cached on mtime/size). While the host is held, every local device is
  treated like a held device: never a placement, eviction or warm target. Measured VRAM/RAM does not
  matter. Hosted backends are not this host's memory and are kept. Resident units are not evicted.
- A facade (`process_node.py`) reads the same file per call. While the host is held its capability
  reports `ready: false` with `detail: "host health held: …"`, and `warm`/`begin` return 503. Fleet
  brokers and the hub therefore stop routing to it. Without this, node-h's speech nodes stayed
  `ready` in the NA fleet view and took a live dictation session on 2026-11-17.
- `/status.host_health` and `/fleet.vantage_host_health` show it.
- Writing or removing the file takes effect without a restart, **once hostd and the facades run a
  release with this change**. An older release ignores the file.

## Tests

- `tests/test_workload_host_health_capacity.py`: RAM increase and decrease, stale heartbeat,
  reboot (stale-boot report), a held host surviving capacity growth, reboots and three days of claims
  ticks, owner-only release, other hosts untouched, a two-thread assignment race, and HTTP end to end
  (a revised RAM report alone changes admission; a hold covers an unregistered worker).
- `tests/test_host_health_gate.py`: fail-closed file rules, the facade copy agreeing with the
  canonical reader, hostd placing nothing and evicting nothing while held, release without a restart,
  measured capacity never reading as health, and the facade reporting `ready: false`.
