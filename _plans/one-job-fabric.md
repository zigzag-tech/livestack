# One job fabric — the workload authority is where every application's heavy stage runs

Owning artifact for three requirements of the umbrella change `hub-and-compute-convergence`
(`~/unchain/openspec/changes/hub-and-compute-convergence/specs/one-job-fabric/spec.md`). The
other two requirements of that capability are owned elsewhere: the jingway adapter by
`~/jingway/openspec/changes/workloads-offload-adapter/`, the unchain handlers and gateway
retirement by `~/unchain/openspec/changes/render-as-workload-handlers/`. Written 2026-09-20.

## Why

The fleet has four job brokers for one GPU pool: Harmony (residency), this authority
(benchday's E2E and release train, `/v1/workloads/` on xc-tower-ubuntu `:8810`, three enrolled
workers), unchain's portable socket.io gateway on zz-tower2 `:3125` (render, TTS, ASR,
delivery, and an `attune.produce_item` spec that nobody serves), and jingway's socket.io
job-fabric adapter (benchday's `ExecutionBackend`, running nowhere). The decision taken
2026-09-20 is that **this authority survives** and the socket.io brokers converge onto it,
because it already has what they would each have to grow:

- principals with a handler allowlist checked at submit (`workloads/http.py:146-156`,
  `model.submission` refuses `handler is not authorized` with 403);
- worker identity from the credential, never from JSON (`http.py:21-37`);
- `priority`, idempotent `key`, `deadline`, `estimate_seconds`, `admit` vs `need` resource
  vectors (`model.py:113-175`), placed by the same pure scheduler the fleet broker uses
  (`placement.py`);
- a CAS for inputs and artifacts scoped to the principal (`object_routes.py`,
  `blob_references.py`), with a mirror for lossy paths;
- durable attempts with fences, leases, heartbeats and completion receipts
  (`store.py:164-233`, `worker.py:208-300`).

What it lacks for three applications and N users is small and named below.

## Requirements (restated from the umbrella; owned here and nowhere else)

### Requirement: The workload authority is the fleet's job fabric
Every compute- or memory-heavy stage an application delegates SHALL be submitted as a job to
the Livestack workload authority (`/v1/workloads/`) under the application's principal, with
the end user's owner string carried in the job's labels. No application SHALL run a second
job broker once its specs exist as handlers.

- Three applications, one authority: attune submits `attune.produce_item`, benchday submits
  `benchday.thumbnail`, unchain submits `unchain.render_chunk` → all three appear in the job
  list under three principals, each placed on a worker advertising that handler.
- A handler the principal does not hold: the attune principal submits `benchday.thumbnail`
  → 403 `handler is not authorized`, no job created.

### Requirement: A GPU-bound handler leases on the worker
A handler that needs a model SHALL admit through `/fleet/admit` from the worker, under the
owner carried by the job, and SHALL release the lease when the job ends. The authority places
by CPU, memory and disk; it SHALL NOT model GPU residency.

- Owner survives the hop: a job labelled `owner=attune:acct_a` synthesises speech on a
  worker → the fleet ledger's Grant for polytts names `attune:acct_a`, not the worker.

### Requirement: Concurrency is bounded per principal
The authority SHALL enforce a per-principal cap on concurrently running attempts, refusing
or queueing beyond it as the principal's configuration says, so one application's backlog
cannot hold every worker.

- A backlog does not starve another application: attune has 40 queued produce jobs, a cap
  of 2, and benchday submits one thumbnail → the thumbnail is placed on the next free worker
  while attune runs at most two attempts.

## Design

### The owner label

`model.submission` gains `labels` in its allowed set: a flat string map, validated by the
existing `labels()` helper, at most 16 entries. One key is reserved: `labels.owner`, the
end user's owner string in the fleet's form `<app>:acct_<id>`. A caller principal gains
`delegate_prefix` (the same word `fleet_auth.Principal` uses): `labels.owner` must start
with it or the submission is refused 403, exactly as `/fleet/admit` refuses an owner outside
a delegating principal's prefix. A principal without `delegate_prefix` may not set
`labels.owner`; its jobs are owned by the principal itself. `store.submit` persists the
labels on the job row (`jobs.labels`, JSON) and `list_jobs`/`get` return them, so a hub can
ask "what is running for acct_a".

Why labels rather than a new top-level field: `selector` is already "what the target must
have" and `payload` is the handler's; a job's *ownership metadata* is a third thing and a
map is the shape that lets the next application add `run_id` or `project` without a schema
change. Why not put the person in the principal: a hub authenticates many people with one
credential; one principal per person would put account provisioning inside the authority,
which is the hub's job.

### The lease on the worker

`worker.execute` (`worker.py:208-260`) already builds the attempt environment
(`HARMONY_INPUT`, `HARMONY_OUTPUT`, `HARMONY_REQUEST`, `HARMONY_ATTEMPT`). It gains:

- `HARMONY_OWNER` = `job.labels.owner` or the principal id, from the assignment;
- `HARMONY_FLEET_URL` and `HARMONY_FLEET_TOKEN` from the worker's configuration — a
  *delegating* fleet principal for the worker whose prefix is the union of the app prefixes
  it serves (an attune-only worker gets `attune:`; a shared worker gets `""`), issued under
  `fleet-caller-identity.md` R.2.

A handler that needs a model calls `POST $HARMONY_FLEET_URL/fleet/admit` with
`Authorization: Bearer $HARMONY_FLEET_TOKEN`, `owner: $HARMONY_OWNER`, `kind`, `regions`,
heartbeats the lease it gets back, and sends `X-Harmony-Owner: $HARMONY_OWNER` on every
engine request under it. `livestack_node.workloads.lease_helper` (new, ~60 lines) does this
for Python handlers; the TypeScript handlers get the same from jingway's adapter package.
The attempt's cleanup releases any lease still held (`store.complete` → the worker's
`_completion_from_exit` calls release with the lease ids the handler wrote to
`$HARMONY_OUTPUT/leases.json`), so a crashed handler cannot hold capacity past its attempt.

Nothing in `placement.py` reads GPU state. A job that wants a GPU box says so with a
`selector` label the worker advertises (`labels.gpu: "rtx3090"`, already the mechanism), and
whether the model is resident is Harmony's answer at run time, not the authority's at
placement time. Two questions, two authorities.

### The per-principal cap

`Principal` (`http.py:21-37`) gains `max_running: int | None`. `placement.place`
(`placement.py`) counts running attempts per job owner before the queued loop and skips a
queued job whose owner is at its cap with `reason="principal at max_running (N)"`, leaving it
queued; the loop continues to the next owner's jobs so a capped principal never blocks the
queue behind it. Refuse-vs-queue is the principal's choice: `on_cap: "queue"|"refuse"`
(default `queue`); `refuse` makes `store.submit` answer 429 naming the count, the same shape
the fleet broker uses. `GET /v1/workloads/jobs` for a caller includes its cap and running
count.

### Three principals on one authority

`authority.json` gains, beside benchday's existing caller and workers:

```jsonc
{"id": "attune-hub",  "role": "caller", "delegate_prefix": "attune:",
 "handlers": ["attune.produce_item", "attune.source_connector"], "max_running": 2},
{"id": "benchday-hub","role": "caller", "delegate_prefix": "benchday:",
 "handlers": ["benchday.thumbnail", "benchday.title_gen", "benchday.asr_finalize",
              "benchday.e2e.full.v1", "…release…"], "max_running": 4},
{"id": "unchain",     "role": "caller", "handlers": ["unchain.render_chunk", "unchain.render_concat",
              "unchain.render_full", "unchain.asset_normalize", "unchain.thumbnail_generate",
              "qwen_tts.synthesize_segment", "qwen_tts.synthesize_batch", "unchain.asr"]},
{"id": "sorbonne",    "role": "caller", "handlers": ["unchain.asr", "qwen_tts.synthesize_segment",
              "unchain.render_chunk"], "max_running": 1}
```

Handlers are installed on workers the way `benchday_handler_release` is today
(`~/benchday/docs/harmony-worker-enrolment.md`): an immutable bundle per application named in
the worker's configuration, never a checkout. The bundle format is unchanged; what is new is
that three applications' bundles sit on one worker.

### What does not change

The wire (`/v1/workloads/jobs`, `worker/{report,claim,heartbeat,complete}`, objects,
references), the durable state machine, the CAS, the mirror, the source archive. A caller
that ignores every field added here behaves exactly as today.

## Tasks

- [x] J.1 `labels` on submission with the reserved `owner` key; `delegate_prefix` on `Principal`; 403 outside the prefix; labels persisted and returned → verify: `tests/test_workload_labels.py` — attune-hub submits with `owner=attune:acct_a` (accepted), with `owner=benchday:acct_b` (403), unchain (no prefix) with any `owner` (403). Done 2026-09-20 (commit `2be3494`, 6 passed; refusals reuse `fleet_auth.resolve_owner` so the two surfaces can't drift; ≤16-label cap enforced in submission after the 64-cap helper).
- [x] J.2 Attempt environment carries `HARMONY_OWNER`, `HARMONY_FLEET_URL`, `HARMONY_FLEET_TOKEN`; `lease_helper` admits, heartbeats, releases; cleanup releases leftovers from `leases.json` → verify: `tests/test_worker_lease_env.py` against a fake fleet broker — the Grant names the label owner; a handler killed mid-run leaves no live lease. Done 2026-09-20 (commit `2be3494`, 3 passed incl. a real-systemd kill test; `lease_helper` is ~100 lines not ~60 because cleanup + progress live there too; cleanup is worker-side — the authority holds no fleet credential — and tolerates a dead broker via lease TTL; a SIGTERM-killed handler requeues by existing semantics, the lease guarantee is what the test pins).
- [x] J.3 `max_running` + `on_cap` on `Principal`; placement skips capped principals without blocking others; 429 on `refuse` → verify: `tests/test_placement_principal_cap.py` — the umbrella scenario (40 queued at cap 2, one thumbnail from another principal placed next). Done 2026-09-20 (commit `2be3494`, 3 passed — the 40 queued jobs carried HIGHER priority than the thumbnail and still never blocked it).
- [x] J.4 Caller job list reports cap and running count → verify: `GET jobs` body has `principal: {max_running, running}`. Done 2026-09-20 (commit `2be3494`, asserted in the cap test file).
- [x] J.5 `durable-workloads.md` gains a "Three applications" section naming the label, the env, the cap and the principal table; `~/benchday/docs/harmony-worker-enrolment.md` gains the multi-bundle note (filed in benchday) → verify: both documents name `labels.owner` and `HARMONY_OWNER`. Done 2026-09-20 for the livestack half (commit `90680e8`); the benchday half of this task moves with benchday's execution change.
- [ ] J.6 Production `authority.json` gains the four principals above; an attune handler bundle and the unchain bundle are installed on `xc-tower-e2e-1` → verify: `python3 -m json.tool ~/.config/livestack-workloads/authority.json` lists four caller principals; the worker's `report` advertises `attune.produce_item` and `unchain.render_chunk`.
- [x] J.7 A progress channel. The authority has none: a caller sees `queued → running → done`
      and nothing between, and the jingway adapter's `onProgress` (umbrella requirement "The
      jingway compute-offload port has a workloads adapter", jingway task W.5) needs a source.
      The worker publishes a small `progress` reference on the attempt (`{phase, detail?,
      fraction?}`, the jingway `JobProgressSchema` shape) through `worker/heartbeat`, the store
      keeps only the latest, and `GET jobs/<id>` returns it → verify:
      `tests/test_workload_progress.py` — a handler that reports `tts` then `stills` is read
      back in that order by a polling caller; a heartbeat without progress leaves the last
      value in place; the field is absent, not null, for a handler that never reports.
      Done 2026-09-20 (commit `2be3494`, 2 passed; malformed progress refused not stored;
      additive wire — old workers/new authorities and new workers/old authorities both fine).

## Gate (Phase B, shared with the jingway and unchain owners)

| Gate | Number to record |
|---|---|
| Three principals, one authority | `GET /v1/workloads/jobs` as admin shows one completed job each for `attune-hub`, `benchday-hub`, `unchain`, with the attune one labelled `owner=attune:acct_<id>` |
| Owner survives the hop | the fleet ledger's Grant for polytts during the attune job names that owner |
| Cap holds | with 40 attune jobs queued and `max_running: 2`, a benchday thumbnail's `created → running` latency ≤ one placement tick |
| Brokers retired | `unchain-gateway.service` inactive; `~/jingway/src/adapters/jobfabric/` deleted (jingway's gate) |
