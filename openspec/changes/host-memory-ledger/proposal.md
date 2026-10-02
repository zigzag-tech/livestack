## Why

On 2026-10-02 00:22–01:58 UTC every `benchday.e2e.full.v1` attempt on **zz-joe**
ended `infrastructure`: hub start took 80–150 s (budget 60 s), daemons missed their
120 s enrollment window, then the client catalog missed its 30 s window. The host was
swapping (17 GiB swapped, IO PSI full avg300 ≈ 3.4). Nothing was OOM-killed; the
102 GB swap file absorbed the overcommit and every startup step crawled. Evidence:
Benchday `docs/e2e-admission-capacity.md`, "2026-10-02: zz-joe's other tenants no
longer fit".

The arithmetic was knowable and Harmony did not do it:

| tenant on the 31 GB host | what Harmony charged | what it really used |
|---|---|---|
| e2e attempt 1 | `admit` 4 GiB | 7.8–10.0 GiB (`memory_peak_bytes` of 20 attempts that night; the cap is 10 GiB) |
| e2e attempt 2 | `admit` 4 GiB | same |
| `harmony-klein-0` (image model server) | nothing | idles ~3.6 GB, peaks **16.0 GB** (`MemoryPeak`) while it moves its text encoder through host RAM |
| polytts / polyasr / embed | nothing | peaks 12.6 / 6.3 / 0.8 GB |

Three structural defects in livestack produced it:

1. **Static ceilings override observation.** `workloads/worker.py` `report()` measures
   `MemAvailable`, disk and `cores − loadavg`, then clamps all of it to a hand-written
   `capacity` per worker identity (zz-joe: 12 CPU / 20 GiB each). The ceiling was
   sized from a stale belief ("other tenants 5–8 GB") and nothing ever checked it.
2. **Admission is a one-time snapshot charged at the wrong number.** Placement
   (`workloads/placement.py`) reserves each running attempt's `admit` vector, not what
   attempts of that handler are known to reach. Each attempt's real peak is already
   recorded in its result (`resources.memory_peak_bytes`) and never read back. Nothing
   reacts to pressure (PSI, swap-in) after admission.
3. **Two planners, one machine, no shared ledger.** `hostbroker.py` plans model servers
   on VRAM only; workload placement plans host RAM/CPU/disk. A model server's host RAM,
   including its load/serve transients, is in neither ledger.

**Design records realised:** `_plans/durable-workloads.md` (workload placement) and
`_plans/resource-planner.md` (multi-resource footprints). **Stale in them:**
`durable-workloads.md` describes worker `capacity` as the description of a host and the
`admit` vector as the whole placement charge; after this change `capacity` is an
optional operator ceiling, memory on a measured host is charged at a learned peak, and
measured host state is part of every placement. `resource-planner.md` §"multi-resource"
lists `ram_bytes` as a future footprint dimension of a unit; this change does not add it
to the GPU planner (see design §6) but does make model servers' host RAM a claim the
workload planner honours.

## What Changes

1. **A per-host measured view** (`livestack_node/hostview.py`, stdlib only): reads
   `/proc/meminfo`, `/proc/pressure/{memory,io,cpu}`, `/proc/vmstat` swap-in, and
   cgroup v2 `memory.current`/`memory.peak` for every Harmony tenant on the host:
   running attempt cgroups (`harmony-work-*-<attempt>.service`) and operator-listed
   model-server units (`host_services`). A model server's host-RAM peak is **learned**
   from its cgroup `memory.peak` and kept across restarts in the worker's state dir.
2. **Workers report it** with every report (idle every ~2 s, busy every 10 s) as a new
   `host` block. `capacity` becomes optional: absent, it is the measured host
   (online CPUs, `MemTotal`, workspace filesystem size).
3. **Placement charges claims, not snapshots, on a measured host:**
   `free memory = MemAvailable − reserve − Σ attempts max(0, claim − current)
   − max over model servers of max(0, learned peak − current)`. An attempt's claim is
   the **learned peak of its handler** (max `memory_peak_bytes` over that handler's last
   20 completed attempts, within `[admit, need]`), falling back to `need` until learned.
   A new job must fit its own claim, not its `admit`.
4. **Pressure defers admission.** A host whose freshest report shows memory PSI
   `full avg60 ≥ 5 %` or swap-in ≥ 16 MiB/s admits nothing; each refused job names it
   (`host memory pressure: …`).
5. **Back-compatible.** A host whose workers do not send `host` is placed exactly as
   before. Existing worker configs keep working; their `capacity` stays a ceiling.

**Not changing:** CPU and disk placement (still `admit`-charged on top of measured
headroom; CPU is compressible and was not the failure); the one-attempt-per-worker-id
rule; the GPU planner (`planner.py`/`hostbroker.py`). Design §6 says why each was left.

## Impact

- `node-py/livestack_node/hostview.py` (new), `workloads/worker.py`,
  `workloads/store.py` (accept and bound the `host` block), `workloads/placement.py`.
- Rollout order matters: the authority must accept `host` before any worker sends it
  (an old authority rejects an unknown report key). Authority first, then zz-joe's
  two workers, then the rest.
- Benchday: none required. `benchday.e2e.full.v1` keeps `need` 10 GiB / `admit` 4 GiB;
  on a measured host the learned peak governs memory, and `admit` still governs CPU/disk
  and legacy hosts.
