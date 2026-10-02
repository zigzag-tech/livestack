# Design — host-memory-ledger

Read first: `node-py/livestack_node/workloads/placement.py` (`place`),
`workloads/worker.py` (`report`), `workloads/resource_usage.py` (the per-attempt
receipt that already carries `memory_peak_bytes`), and Benchday
`docs/e2e-admission-capacity.md` (the 2026-10-02 section).

## 1. Who owns what, and how ids cross (config rule)

| durable state | owner | where | written by |
|---|---|---|---|
| Measured host block (`report.host`) | authority | `workers.report` JSON, one row per worker identity, replaced each report | each worker, from `hostview.HostView.sample()` |
| Learned model-server peaks | worker | `<state_dir>/host-peaks.json`, `{cgroup path: bytes}`, ≤ 32 entries (one per configured service) | `HostView` after each sample; only ever raised |
| Learned handler peaks | authority | derived on each placement pass from `attempts.result` (`$.result.resources.memory_peak_bytes`) — no new table | nothing new: the worker's existing completion receipt |

Ids crossing the boundary: an attempt cgroup is named
`harmony-work-<sha256(worker)[:16]>-<attempt id>.service` (`supervision.py`). The worker
reports `{attempt id: memory.current}` for **every** such cgroup in its user manager's
`app.slice`, including its sibling identities' attempts; the authority joins that on
`attempts.id` and ignores ids it does not hold as active. Model servers are reported by
their cgroup path (`system.slice/harmony-klein-0.service`); the authority never needs to
resolve them, only to charge them.

## 2. Measurement (`hostview.py`)

Stdlib only (workers run on the system `python3`). Every reader takes a root
(`proc`, `cgroup_root`) so tests drive it with real files in a temp tree.

- `memory_total_bytes`, `memory_available_bytes` — `/proc/meminfo`.
- `psi` — `{memory, io, cpu}` → `{some_avg10, some_avg60, full_avg10, full_avg60}` from
  `/proc/pressure/*`. A kernel without PSI reports `None`, never zeros.
- `swap_in_bytes_per_second` — `pswpin` delta × page size over the time since the
  previous sample. `None` on the first sample (unknown is never zero).
- `attempts` — `{attempt id: memory.current}` for every attempt cgroup.
- `services` — `{path: {current_bytes, peak_bytes}}`; `peak_bytes` is the max of the
  cgroup's own `memory.peak` (since unit start) and the persisted learned peak, so a
  restart (which resets `memory.peak`) does not forget a 16 GB transient. A configured
  service whose cgroup is absent (unit stopped) reports `current_bytes: 0` and keeps
  its learned peak: it may come back and load.
- `memory_reserve_bytes` — the worker's configured reserve (default 1 GiB).

## 3. Placement on a measured host

For each host, the freshest fresh report that carries `host` is the host's view (each
worker on a host measures the same machine; the newest reading wins). Then:

```
claim(handler, admit, need) = clamp(learned_peak(handler), admit.mem, need.mem)
                              if learned else need.mem
free_mem(host) = memory_available − memory_reserve
               − Σ active attempts a on host: max(0, claim(a) − current(a))
               − max over services s: max(0, peak(s) − current(s))
```

Why each term:

- **`memory_available` already contains every tenant's current use.** Only the part of
  each claim not yet realised (`claim − current`) is still to come, so charging the
  difference does not double-count. An attempt admitted a moment ago has no cgroup yet:
  current 0, charged in full. This replaces the `admit`-based subtraction for memory;
  today's code subtracts `admit` from a figure that already contains the attempt's real
  use, which over-charges early and under-charges late.
- **Learned handler peak, within `[admit, need]`.** `need` is the cgroup `MemoryMax`, so
  no attempt can exceed it; `admit` is the caller's stated floor. The statistic is the
  max of the last 20 recorded peaks of SUCCEEDED attempts: it tracks a handler that grew
  or shrank within a day of traffic and needs no decay knob. Only successes teach, because
  an attempt that died in preparation peaks low: at deploy time (2026-10-02 02:40 UTC)
  the last 20 e2e attempts with a peak were mostly infrastructure failures at 2.5-4 GiB,
  which would have taught 7.8 GiB, while the last 20 successes reach 10 GiB. For e2e that night it is 10 GiB
  (attempts hit their cap), so two attempts cannot both fit beside klein's transient.
- **Model servers: the largest outstanding transient, not the sum.** zz-joe's servers'
  learned peaks sum to ~36 GB on a 31 GB host; they are load/serve transients of
  independent servers, and nothing so far shows them coinciding.
  Charging the sum would admit nothing, ever. Charging the largest
  `peak − current` keeps room for any one server's next spike; coincident spikes are
  what the pressure gate (§4) catches. A server that is mid-spike has a small
  `peak − current`, but its usage is then already in `memory_available`.
- **Fit test uses the new job's claim, not its `admit`.** An e2e job that will grow to
  10 GiB must find 10 GiB, or the overcommit simply moves later.
- The worker's `capacity.memory_bytes`, when configured, still caps what that identity
  may take (operator override). CPU and disk are unchanged (measured headroom minus
  `admit` reservations).

Hosts with no `host` block are placed exactly as before (back-compatibility for workers
not yet upgraded, e.g. the Mac guest and WSL during rollout).

## 4. Pressure gate

A host is refused outright when its view shows memory PSI `full_avg60 ≥ 5.0` or
`swap_in_bytes_per_second ≥ 16 MiB`. Each compatible worker's refusal reads
`host memory pressure: memory full avg60 7.1% (limit 5%)` (or the swap figure), so the
queued job's reason names it. `avg60`, not `avg10`, so one burst does not flap
admission; a 16 MiB/s swap-in is ~4k pages/s, well past "an idle server faulting back
in" (zz-joe at rest: < 10 pages/s) and well under the incident's thrash. IO PSI is
reported, not gated: an e2e attempt's own image build legitimately saturates IO, and
gating on it would make a host refuse work because it is doing work. These are two
constants in `placement.py` beside `AVOID_SECONDS`, not configuration.

The gate only defers new admissions. Running attempts are never stopped by it.

## 5. Ledger obligation

Workload placement's ledger is the job row's `reason` (existing). New reasons:
`host memory pressure: …` and `insufficient shared host resources` now carries the
memory arithmetic when memory is the binding dimension:
`insufficient host memory: claim 10.0 GiB > free 6.2 GiB (available 21.0, reserve 1.0,
attempts 0.0, model servers 13.8)`. The figures are in the refusal so an operator can
check them against `free`/`systemctl show -p MemoryPeak` without reading code.

## 6. Rejected alternatives

- **Raise zz-joe's `memory_reserve_bytes` to ~16 GiB.** Fixes one host by hand with the
  same static number that went stale last time; wrong the day klein is stopped or grows.
- **Cap klein with `MemoryMax`.** Its 16 GB is real work (text encoder offload); a cap
  turns it into an OOM-killed image job. Harmony's job is to account for it.
- **Charge `need` instead of `admit` everywhere.** Correct for e2e that night, but
  over-charges handlers that never approach their cap; the learned peak gets the same
  answer where it is true and a smaller one where it is not.
- **Multi-attempt workers (concurrency from fit instead of identities).** The defect was
  over-admission, not too few slots. With this change an identity is only an upper
  bound; whether a second attempt starts is decided by measured fit. Making one worker
  process run several attempts touches the journal, lease keeper, cleanup and restart
  recovery — a separate change with its own risk, deferred.
- **Gate GPU model loads (`hostbroker`) on host pressure in this change.** A model load
  deferred because a batch job made the host swap inverts the priority: interactive
  inference should win, batch should yield, and with §3 batch no longer creates that
  pressure. The seam for a later change is a `host` input to the GPU planner's world
  (pure `plan()`), not a dispatch-time skip that would leave a Grant without its Load.
- **IO PSI gate** — §4.

## 7. Rollout

1. Authority (xc-tower-ubuntu, `livestack-workload-authority.service`): stage a release
   per Benchday `docs/harmony-worker-enrolment.md` ("Rolling the AUTHORITY code"); it
   accepts and uses `host` but sees none yet — behaviour unchanged.
2. zz-joe workers (`livestack-workload-worker{,-2}.service`): new release, configs gain
   `host_services` (the GPU/model units) and drop `capacity` and the 4 GiB reserve (the
   reserve existed for the model servers, which are now claims). Restart only idle.
3. Verify (§8), then roll the remaining Linux workers; the Mac guest keeps its
   `host_pressure` file (the guest's `/proc` does not see the Mac) — its `host` block is
   still sent and its `available` stays clamped by the host file.

Rollback: repoint the drop-ins to the previous releases. The authority ignores `host`
when no report carries it, so a mixed fleet is safe in either direction once the
authority is upgraded.

## 8. Verification

- Tests (real files, real SQLite): `tests/test_hostview.py` drives the readers against a
  temp `/proc` + cgroup tree (PSI absent → `None`; swap rate from two samples; learned
  service peak survives a reset `memory.peak`); `tests/test_workload_host_memory.py`
  drives `WorkloadStore` placement with real reports: the 2026-10-02 shape (two e2e
  claims + klein transient on 31 GB) admits one attempt, not two; a learned peak below
  `need` admits where `need` would not; pressure refuses with a named reason; a legacy
  host is unchanged. Positive control: the overcommit test fails on the old placement.
- Live: worker reports carry `host`; with klein resident zz-joe admits one e2e attempt
  and the second job's reason names the memory arithmetic; attempts that run there
  start the hub well under the 60 s budget; job outcomes before/after from the
  authority.

## 9. Corrections after the first deploy (2026-10-02 ~03:00 UTC)

- **Measure non-reclaimable memory, not memory.current/memory.peak.** After a load,
  klein-0 read memory.current 17.8 GB with anon 1.36 GB and no swap: the "16 GB peak"
  was the 14.8 GB of safetensors in page cache, which MemAvailable already counts as
  available. Charging it again double-counted it. Services' and attempts' figures are now
  `anon + shmem + (kernel - slab_reclaimable)` from memory.stat; a service's learned peak
  is the max of those samples (one per report), persisted under a new file name
  (`host-peaks-nonreclaimable.json`) so cache-inflated values are not inherited. The
  worker samples each attempt's figure every ~0.2 s and records
  `memory_nonreclaimable_peak_bytes`; placement learns from it, falling back to
  `memory_peak_bytes` only while no succeeded attempt carries it.
- **A spike is outstanding only while a unit is not resident.** A `host_services` entry
  may be `{path, residence}`; the worker reads `/livestack/residence` (1 s timeout,
  256 KiB cap) and reports `resident`. Placement skips a service's spike only on an
  explicit `true`; `false` or an unreadable server (`null`) is charged.
- **`serve.attach()` drives idle eviction.** Nothing called `manager.maybe_evict()` for
  servers that did not write their own loop (klein stayed resident 48+ min past
  `idle_seconds=900`). attach() now starts the sweep (through `gpu_call`, skipped while
  `in_flight` > 0, every min(idle/4, 30) s, failures logged and never fatal); the embed
  node's own loop is removed. Servers that still call `maybe_evict()` themselves
  (polyasr) sweep twice; that is idempotent under the manager's guard.
