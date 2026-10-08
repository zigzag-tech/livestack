## Why

Every storage bound in the workload authority and worker is an absolute number chosen
by hand, and none knows how much disk the machine has left. On 2026-10-06/07:

1. **A quota larger than the headroom.** The authority's content-store objects directory
   held 195 GiB under a 210 GiB `blob_limits.max_bytes`, on a root disk 97 % full
   (about 50 GB free) shared with Docker, models and worktrees. The quota was "within
   bounds" while the disk was about to be full. `BlobStore.put` only compares against
   `max_bytes` / `max_objects` (`blobs.py`); it never looks at the filesystem.
2. **A silent stall.** The release stager (`xc-tower-stager`) reported disk 0 / 192 G
   available because its `disk_reserve_bytes` (64 GiB) exceeded the free space
   (`worker.py report()`: `available.disk_bytes = max(0, min(capacity, free - reserve))`).
   Every release `prepare` job waited 20+ minutes with the refusal
   `insufficient shared host resources` (`placement.py`), which names neither disk nor
   the figures; the real reason was in a worker log line.
3. **Retention that is flat.** Terminal jobs are kept the same time whether they
   succeeded or failed; 130 `benchday.release.*` `blob_references` never expire
   (about 20 GiB) because no `reference_expiry` rule covers them; and the docs
   (`docs/daemon-storage-bounds.md` in Benchday) list a stale 200 GiB bound.
4. **A CPU signal that says no when the machine is merely busy.** `cpu_admission`
   `loadavg` reports 0 available when load exceeds cores (the 6-core tower runs load 22-28
   as a matter of course). The `psi` policy that replaced it gates on
   `/proc/pressure/cpu` **`full`** `avg60`, and on this kernel `full` reads 0 even under
   heavy load (CPU `full` is only populated on newer kernels / for cgroups), so the
   gate can never fire: an admitted job on a saturated host is not refused either.

**Design records realised:** `_plans/durable-workloads.md` ("hard bounds"),
`node-py/docs/worker-cpu-admission.md`, `openspec/changes/host-memory-ledger` (memory
analogue: measured headroom beats a static ceiling). **Stale:** durable-workloads
describes the blob bound as one absolute number; worker-cpu-admission documents `full
avg60` as the stall signal.

**Concurrent work (do not duplicate or edit):** another agent is changing the authority's
retention *values* in `authority.json` and Benchday's `docs/daemon-storage-bounds.md`.
This change adds the *mechanism* (headroom-derived bounds, tiers, refusals, signals) and
reads those values; it edits neither file.

## What Changes

1. **Effective bounds are computed in one place.** A new `workloads/storage_bounds.py`
   turns configuration into the effective byte bound for each store as
   `min(absolute cap, fraction x filesystem capacity)` and a **free-space floor**
   (`headroom_bytes` / `headroom_fraction`, the larger applies). The result, with the
   inputs, is logged at startup and whenever it changes, and served in status.
2. **Admission below headroom refuses by name, after a GC pass.** `BlobStore.put` /
   `put_range`, upload grants, and job admission consult the effective bound. Below the
   floor the authority first runs one bounded proactive GC pass (retention + expiry,
   oldest/lowest-tier first), re-reads free space, and only then refuses:
   `storage_headroom: objects filesystem has 38.1 GiB free, floor 40 GiB (after GC freed
   2.0 GiB)` with HTTP 507. Never a bare 429 or a generic reason.
3. **Worker disk refusals are first-class.** When `available.disk_bytes` is clamped to 0
   (or below a job's need) by `disk_reserve_bytes` / `backing_reserve_bytes`, the worker
   report carries `disk_unavailable: {free, reserve, capacity, filesystem}` and placement
   names it: `worker <id>: disk 0 of 192 GiB offered (free 60 GiB < reserve 64 GiB)`.
   A reserve larger than the filesystem's free space is also a startup/reload warning
   event, and a roster flag `reserve_exceeds_free`.
4. **Tiered retention.** Replace the single flat window with tiers per terminal outcome
   (`succeeded`, `failed`, `cancelled/expired`) for jobs/attempts/results, and an
   explicit `{ttl_seconds, keep_last_n}` policy per `blob_references` owner/prefix
   (extends existing `reference_expiry`; today's rule shape `keep_newest, min_age_seconds`
   is kept as the compatible subset). Release references get an explicit rule; there is
   **no implicit "never"**: an owner/prefix with no rule is reported in status as
   `unbounded_references` with count and bytes, so unbounded growth is visible.
5. **Free-space alert surface.** Status and the roster show, per authority and per worker,
   filesystem free bytes/fraction, the effective bound, the floor and the state
   (`ok`, `low` below `alert` threshold, `refusing` below floor). `low` raises a bounded
   event once per state change.
6. **CPU admission on a correct signal.** `cpu_admission` gains policy `psi_some`
   (`/proc/pressure/cpu` `some avg10`, which this kernel does populate) and `runqueue`
   (`procs_running` per core from `/proc/loadavg` / `/proc/stat`, sampled over a short
   window). The policy is validated at startup with a **self-test positive control**: the
   worker spawns a short synthetic CPU burn (cores+1 busy threads for a bounded
   interval) and checks the chosen signal moved; if it cannot move, the worker refuses to
   report `cpu` under that policy, says so by name, and does not fall back silently to
   `loadavg`. `psi` (`full`) stays valid but is documented as unsuitable where `full` is
   always 0 and flagged `signal_inert` by the same self-test.
7. **Admission refusal reasons are status.** Every placement refusal reason and the
   per-worker offered/need figures appear in `status` and the roster as structured fields
   (`reason_code`, figures), not only in free text on the job.

**Not changing:** the retention values and `docs/daemon-storage-bounds.md` (concurrent
agent); the content-addressed layout; handler release registry bounds (burst headroom
already has its own); GPU planning.

## Impact

- New: `workloads/storage_bounds.py`.
- Modified: `blobs.py`, `blob_references.py` (tiers/TTL), `store.py` (tiered job
  retention, status), `placement.py` (named disk refusals), `worker.py` (report
  `disk_unavailable`, cpu self-test), `cpu_admission.py` (new policies, schema),
  `config.py` (`storage_bounds`, `retention_tiers`), `roster.py`, `http.py` (507, status),
  `node-py/docs/worker-cpu-admission.md`, `_plans/durable-workloads.md`.
- Config: new sections are schema-validated (`extra=forbid`); absent sections keep
  today's behaviour exactly (back-compatible); invalid ones fail closed at startup and on
  SIGHUP.
- Rollout: authority first (accepts new report keys), then workers. Nothing is deleted
  by merely upgrading; destructive tiers need an explicit config section.
- Benchday follow-up doc lists the doc corrections and the stager reserve change.
