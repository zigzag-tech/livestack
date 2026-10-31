# Design

## Context

See `proposal.md` for the motivation. `_plans/durable-workloads.md` already
defines the authenticated principal as the workload owner and the WorkloadStore
as the job authority. The stream intent ledger records a submit body before it
is dispatched; `WorkloadStore` persists the admitted job and its attempts; the
fixed-owner `jobs/changes` reader is the only source suitable for a
read-only graph join. Attempt claims already bind both worker id and worker
boot id.

The missing information crosses three durable boundaries: the authenticated
submit body must carry the Overlord turn source; the job row must preserve it
through authority restart; and the owner reader must return that exact source
alongside the latest attempt. Existing completion results and handler progress
can contain arbitrary text, so the new lineage projection must use safe typed
explanation codes instead of exposing that text.

## Goals / Non-Goals

**Goals:**

- Keep the authenticated principal as the only owner authority.
- Preserve an optional exact turn id as immutable job provenance, separate
  from labels and routing inputs.
- Let a fixed-owner reader find only jobs for that exact turn with bounded,
  indexed reads and an explicit incomplete-page signal.
- Return the authority-issued job and attempt ids, authenticated worker boot
  session, and a safe current state explanation suitable for the client graph.
- Preserve existing submissions and jobs that predate the optional source.

**Non-Goals:**

- Selecting or retrying jobs, workers, routes, models, or gateways.
- Inferring lineage from labels, handler kind, timestamps, similar payloads,
  pane names, or worker roster proximity.
- Returning logs, artifacts, command output, transcript content, credentials,
  or arbitrary handler progress through the lineage reader.
- Adding a second job store or a placement-decision ledger entry.

## Decisions

### Put the optional source on the authenticated submit intent

The stream submit body accepts only the closed object
`source: {kind: "overlord_turn", turn_id}`. Validate the kind, bounded id, and
exact object keys at the stream authority before recording or forwarding the
intent. A new `source_turns` capability on the operator-configured workload
principal defaults to false and is allowed only for caller/admin principals.
The stream adapter and direct jobs endpoint both enforce it before job
admission. The authority continues to derive owner from the authenticated
principal; `source` is provenance and never chooses an owner or admission
policy. The existing intent ledger records the validated field before backend
dispatch. Reusing an intent id with a different source changes the recorded
body and is rejected by the existing idempotency conflict rule.

**Alternative:** place the turn id in `labels`. Rejected because labels are
caller-facing display metadata and already participate in delegated-owner
rules; using them as causality would let presentation data impersonate the
source authority.

### Persist provenance as nullable job columns

`WorkloadStore` adds nullable `source_kind` and `source_turn_id` columns to the
jobs table, with an additive open-time migration. Existing rows remain
unlinked. The source values also remain in the canonical job spec used for
idempotency hashing, so a replay with another source conflicts rather than
replacing provenance. Add an index beginning with `source_turn_id` for exact
turn lookup. Do not backfill legacy rows from labels, timestamps, or handler
metadata.

The workload authority's intent ledger owns pre-dispatch intent identity and
body. `WorkloadStore` owns admitted job provenance and execution state. The
worker owns neither: it cannot set or mutate the source id. The attempt row's
`boot` value is written from the authenticated claim and is the exact worker
process-session id for that attempt.

**Alternative:** reconstruct the join by scanning intent bodies or parsing
job specs at read time. Rejected because it bypasses the admitted job owner,
adds history-sized work, and cannot efficiently enforce the fixed-owner query
bound.

### Extend the fixed-owner reader with an exact source filter

Add a bounded `source_turn_id` query parameter to the existing
`jobs/changes` reader. Its SQL applies both the authenticated fixed owner and
the exact source id before ordering and limiting, and uses the source index.
The existing `(updated_ms, job_id)` cursor and 256-row / 30-KiB response bounds
continue to apply. A complete empty result is `jobs: []` with `truncated: 0`;
any withheld matching row sets `truncated` and returns the existing cursor.
Consumers must treat every truncated page as incomplete, even if the first
page contains a plausible match.

Rows in exact-source mode include the source id, job id, latest authority-issued
attempt id, worker id and attempt boot id. They include a separate
`current_explanation` with a closed code derived only from authority-owned
state (`queued`, `running`, `succeeded`, `failed`, `cancelled`, or `expired`)
and the job's `updated_ms`. Do not copy handler `result`, `progress`, error
prose, or artifact data into this field. The existing broad reader projection
remains compatible; the exact-source projection is intentionally smaller and
privacy-safe.

**Alternative:** make the client query the worker roster and correlate by
worker id. Rejected because a roster is not evidence that a worker executed
this turn's job and can select a later session after a retry.

### Validate the exact owner response in WorkloadClient

`WorkloadClient` exposes a dedicated exact-turn method taking an explicit
`expected_owner` and `source_turn_id`. It validates response owner, byte and
row bounds, cursor consistency, source-id equality on every returned job,
unique job and attempt identity, and the closed explanation schema. It does
not return a complete result when any page is truncated or malformed. The
caller matches the returned source id byte-for-byte to the authenticated
Overlord turn id; absent, mismatched, multiple-ambiguous, or incomplete data
stays `unknown` in Benchday.

The API remains optional and additive: old submitters omit `source`, and old
readers continue using the unfiltered changes projection. A source-aware
consumer never silently falls back to that broad projection.

**Alternative:** expose one unvalidated dictionary and ask each consumer to
interpret it. Rejected because malformed or cross-owner data could become a
false graph edge at a consumer boundary.

## Risks / Trade-offs

- [Old databases contain no lineage] → Leave migrated columns null and report
  those jobs as unlinked; never guess a historical source.
- [One turn submits more jobs than one reader page can hold] → Preserve the
  bounded cursor and truncation signal; consumers retain unknown until the
  exact result is complete.
- [Worker restart changes process identity] → Read the attempt's authenticated
  boot id, not the worker's current registration, and return the boot id stored
  with the exact attempt.
- [Failure details are useful to operators] → Keep existing operator views;
  the exact graph reader emits only a closed state code and timestamp.
- [Optional fields are omitted by older callers] → Keep both fields nullable
  and leave legacy graph relations unknown.

## Migration Plan

1. Add closed source validation and backward-compatible schema migration.
2. Persist the source with the job and index exact-turn lookups.
3. Extend owner-bound reads and WorkloadClient validation without changing
   admission or placement.
4. Add restart, idempotency, exact-owner, exact-attempt and truncation tests;
   update `HARMONY.md` and `_plans/durable-workloads.md` with the source and
   privacy contract.
5. Deploy the authority before enabling Benchday's source-aware read. Rollback
   by disabling the new reader call; nullable source columns and old requests
   remain valid and need no destructive migration.

## Open Questions

None. The new source is optional, owner remains authenticated, and incomplete
lineage remains unknown as required by the proposal and specs.
