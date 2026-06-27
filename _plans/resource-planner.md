# Resource Planner — generalized priority-preemptive placement for Livestack

**Status:** core implemented + unit-tested (`node-py/livestack_node/planner.py`,
`tests/test_planner.py`); federation transport + service wiring pending.
**Motivation:** three model servers (polyasr 10 GB, polytts 8.75 GB, chipgen 5 GB)
oversubscribed one 24 GB GPU; a long align OOM'd with 233 MB free. Stopping a
service by hand fixed it. That manual `腾挪` (make-room) + `defer` (时间换空间) is
exactly what a planner should do automatically — and not just for GPUs.

---

## 1. The one idea

> **`plan(WorldState) -> Plan` is a pure function.** A `WorldState` can describe one
> GPU, one host, or the whole mesh. Federation only changes how the WorldState is
> *assembled* and how the resulting actions are *dispatched* — never the brain.

That is what makes this a generalized Livestack capability and not a GPU hack. The
planner is transport-free, device-free, clock-injected; it is tested entirely with
fakes. The same function governs all three scales of `restful-model-node.md`:

```
 standalone            per-host gateway              mesh
 one device            one host's devices            all hosts' devices
 WorldState = local    WorldState = host ledger      WorldState = federated union
 dispatch = in-proc    dispatch = local evict/warm   dispatch = RPC to owning host
```

## 2. Generalized over resources

A footprint / capacity is a **vector of named scalar dimensions**, not a byte count:

```
footprint = {"vram_bytes": 10e9}                     # today
          = {"vram_bytes": 10e9, "ram_bytes": 4e9}   # multi-resource
          = {"npu": 1, "license_slots": 1, ...}      # tomorrow
```

`_fits(need, free)` holds iff it fits on **every** dimension, so the binding
constraint is whichever runs out first. GPU residence is just the first tenant.

### Critical correctness note (the actual OOM cause)
The OOM was **activation** memory (a 270 s align chunk needed 522 MB *transient*),
not weights. So:
- `Unit.footprint` MUST be `measured_weights + peak_activation_headroom`, not just
  resident weights. Measure `torch.cuda.max_memory_allocated` delta on first real
  run and cache it.
- `Device.reserved` carries permanent slack the planner never allocates.
- Grants are verified against **real** free VRAM (NVML) before load — trust but
  verify; other tenants (a stray notebook, gnome-shell) exist.

## 3. Residency tiers (mirror `polycore.ResidencyPolicy`, kept dep-free)

| Tier | Example | Planner behaviour |
|---|---|---|
| `HARD_PIN` | ASR | keep >= `min_resident` warm; never preempted; never evict last replica |
| `SOFT_PIN` | TTS | preferred-warm but **preemptible**; restored after pressure settles (debounced) |
| `UNPINNED` | chipgen | demand residence; first evicted, last restored |

Priority is an explicit int (lower = more important); `residency` controls
warm-restore + last-replica protection. ASR=10 / TTS=20 / chipgen=30 reproduces
"TTS yields to ASR, returns when ASR settles; chipgen waits."

## 4. The algorithm (greedy, deterministic)

1. **Order demand** by *effective* priority (base priority improved by **aging** so
   waiters cannot starve), then FIFO.
2. **Place each request** at the cheapest feasible device under one cost function:
   `warm-resident (0)` < `load-into-free (reload_cost)` <
   `load-after-preemption (reload_cost + Σ victim reload + busy penalty)` — plus a
   locality penalty for placing off the input's host. This single comparison is the
   **place-vs-preempt** (`腾挪`-here vs migrate-there) decision.
3. **Preempt minimally**: evict the smallest set of resident units that are strictly
   lower priority, not `HARD_PIN`, past `min_residency_s` (anti-thrash), and — by
   default — **idle**. A unit holding an active (heartbeating) lease is never
   preempted; if room can't be made, the request **defers** (时间换空间).
4. **HARD_PIN floor**: guarantee the warm minimum, preempting idle lower-priority if
   needed (mandatory).
5. **SOFT_PIN restore**: bring preferred-warm units back when pressure has settled
   (`restore_debounce_s`) and there is free room — **never** by preempting.

Anti-pathology guards: `min_residency_s` (no load→evict→load churn),
`restore_debounce_s` (no flap on every gap), aging (no starvation).

## 5. How it sits on what already exists

- **`polycore.Coordinator`** — the seam. A new `PlanningCoordinator` (livestack
  side) builds a single-host `WorldState` from its `ModelManager` units + device
  descriptor, runs `plan()`, applies `Evict` via `manager._evict` before `_load`.
  `on_evict_request` / `report_busy` are already the preemption + busy signals.
- **`node-py/lease.py` (`CapabilityLeaseStore`)** — leases ARE the busy/idle truth
  (`active_kinds`, heartbeats, TTL). A request = a lease request; busy = an
  unexpired non-`__usage__` lease.
- **TS `core/src/capabilities.ts` + `vault` `CapacityManager`** — orthogonal axis.
  CapacityManager plans *job/worker counts per spec across instances*; this plans
  *which units are resident where by resource bytes*. They compose: CapacityManager
  decides "spin N workers"; the planner decides "and they fit like so / preempt so."
- **`gateway` `ServiceLeaseManager` remote** — the federation transport (phase 3):
  swap the in-proc lease store for a `RemoteServiceLeaseManager` and the host
  ledgers federate; the planner runs on the union.

## 6. Federation (phase 3)

- Each host runs a **WorldSource**: reports its devices (capacity/reserved/labels),
  current placements, busy flags, and queued requests.
- A **domain planner** (one authority per resource domain) unions the WorldSources
  into one `WorldState`, runs `plan()`, and dispatches actions to the **owning
  host** (`Evict`/`Load` via that host's coordinator RPC; `Grant` routes the lease).
- **Soft state**: the planner holds no durable DB; hosts are ground truth and
  re-announce placements + leases on reconnect. Heartbeat timeouts reap dead hosts.
- **One authority per device** prevents two planners double-placing on one GPU; the
  domain may be one host (degenerate) up to the whole mesh.

## 7. Rollout

1. ✅ **Core**: `planner.py` + tests (multi-resource, minimal-victim preemption,
   busy protection, cross-device place-vs-preempt, HARD_PIN floor, SOFT_PIN
   debounced restore, aging, anti-thrash).
2. **Single-host wiring**: `PlanningCoordinator` + real footprint measurement
   (weights + peak activation) on polyasr/polytts/chipgen. Kills the manual stop.
3. **Federation**: WorldSource + domain planner over the `ServiceLeaseManager`
   remote seam; action dispatch RPC. Place-vs-preempt across the mesh.

## 8. Open questions

- Busy preemption: ever interrupt active lower-priority work for a much-higher
  request, or always defer? (`allow_busy_preemption` flag, default off.)
- Domain boundaries: one planner for the whole mesh, or per-host with escalation?
- Cost weights (`reload_cost`, locality, busy penalty) — learned or hand-tuned?
- Footprint drift: re-measure peak activation as inputs grow (long audio), or
  reserve a worst-case envelope per unit?
