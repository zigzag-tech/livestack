## Context

The current host broker already owns hosted workload leases and can observe explicit caller outcomes on release. Its `/status` handler also refreshes each peer once and returns the unit reports, including the optional `queue.waiting` gauge. The design extends those existing observations; see proposal.md and the `harmony-engine-units` delta.

The implementation realizes `_plans/fleet-broker.md`'s host status surface. That design's gap inventory predates the current broker and does not specify these bounded per-unit measurements.

## Goals / Non-Goals

**Goals:**

- Provide short-lived, process-local outcomes and load summaries for the fleet lens.
- Preserve the existing decision ledger as the record of placement and policy decisions.
- Keep unknown outcomes and unavailable queue/latency values distinguishable from zero.
- Keep telemetry storage and status projection bounded.

**Non-Goals:**

- Persist telemetry across broker restarts or create a second event ledger.
- Infer a workload result from an expired lease, a failed warm dispatch, or a missing status report.
- Add per-account or per-owner telemetry dimensions.
- Change planning, admission, eviction, or the unit server's queue policy.

## Decisions

### Broker-owned bounded state

`HostBroker` owns counters and latency samples in memory, under a small telemetry lock. Counter state is keyed by unit kind and uses a sparse representation of the current 60 one-minute buckets. Bucket starts and `observed_from_s` use UTC epoch seconds; lease and planning clocks remain monotonic and are not reused as public timestamps. A kind records `observed_from_s` when it first becomes known; buckets before that point are absent. A bucket count saturates at JavaScript's maximum safe integer and carries a saturation marker. The broker tracks at most 64 kinds and exposes a `truncated_unit_kinds` flag if more appear. Each kind keeps at most 256 recent valid job-duration samples from zero through 365 days. Restart resets the state and the `observed_from_s` values make that loss visible.

This state is separate from `policy_runtime` and the decision ledger. The existing grant and release-outcome ledger writes remain authoritative; telemetry hooks do not emit new ledger events.

### Event meanings

- `admitted` increments when an endpoint returns a placement grant for a caller request. A refusal or a speculative plan does not increment it.
- `completed` and `failed` increment only when the caller explicitly releases its lease with `status: ok` or `status: failed`. Expired leases and releases without an outcome remain unknown.
- `evicted` increments after the owning peer accepts an eviction dispatch. A planned, disabled, or failed eviction does not increment it.
- `job_wall_s`, when supplied and validated by the release boundary, is converted to milliseconds and added to the unit kind's recent latency sample. Percentiles use nearest-rank over the retained samples. Without samples, percentile fields are absent.

### Reuse peer snapshots for queue depth

The `/status` handler already refreshes each peer to build its `peers` response. It derives `queue_depth` from those same snapshots by summing each matching unit's non-negative `queue.waiting` value. It makes no second request. If a peer refresh fails, the handler marks queue depth incomplete and omits queue gauges because the missing peer could serve any kind. If a matching unit omits or malforms its queue, that kind's gauge is omitted. A valid zero is preserved.

### Bounded response

`counters` contains the bucket width, window, the truncation flag, and at most 64 `by_kind` entries. Each entry contains at most four 60-item bucket lists, one queue gauge, and two percentile values plus the sample count. Queue gauges are omitted when incomplete; empty history is represented by an empty bucket list and an `observed_from_s` timestamp, not by fabricated zero history.

The existing `/status` response remains otherwise unchanged. Peer refresh errors continue to appear in the existing `peers` array; counter projection must not turn a successful `/status` response into a failure.

## Risks / Trade-offs

- Caller-reported completion and duration are unavailable for leases whose clients do not send release outcomes. Those values stay absent rather than being inferred.
- A peer outage makes aggregate queue depth unknown for the snapshot; omitting it can hide a known partial total, but prevents presenting that partial total as complete.
- Percentiles summarize only the latest 256 reported durations per kind, so they represent recent traffic rather than an exact full-history distribution.
- A 64-kind limit can omit unusual deployments; `truncated_unit_kinds` makes that loss visible while bounding memory and response size.

## Migration Plan

No migration is needed. Deploying a new broker begins fresh in-memory buckets; rollback removes the additional `/status.counters` field and leaves the existing ledger and lease behavior intact.
