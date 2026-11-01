## Context

See proposal.md - Why. The worker's shared normal-execution and recovery path receives the supervisor's exit receipt. `WorkloadStore` owns durable jobs, attempts, result records, causes and reasons. `WorkloadAuthority` owns the per-intent progress ledger and maps the durable job to `harmony.workload/1`; the intent id crosses that boundary as the WorkloadStore request key, while the job id identifies the owner-scoped result fetch. The result route builds the bytes whose digest is published as the output reference.

The current result receipt already carries `exit_code` and resource counters, but the store's `succeeded` cause is empty and its job result has no success attribution or reason. The stream and result manifest are separate readers of that job record and must agree byte-for-byte on terminal classification.

## Goals / Non-Goals

**Goals:**

- Let only a complete, explicit clean-exit receipt support a newly committed successful outcome.
- Derive one bounded, stable success classification from that receipt in the WorkloadStore transaction, then persist it with the canonical job result.
- Make the ordinary job view, stream partial and fetched manifest expose one classification and cause evidence.
- Preserve historical successful jobs with incomplete receipts as `unknown` with an explicit reason.

**Non-Goals:**

- Change admission, placement, retry, cancellation, or owner authorization policy.
- Add a handler-authored success narrative or accept arbitrary worker-supplied attribution text.
- Add a database column or a new placement-decision ledger entry; the current JSON result, cause and reason fields are sufficient, and this work makes no placement decision.

## Decisions

1. **Classify success from the supervisor receipt at the durable owner.** `WorkloadStore.complete` will require an integer `exit_code` equal to zero and explicit non-negative integer zero values for `resources.oom_kill` and `resources.pids_max_events`. Booleans, missing fields, malformed values, `resource_evidence: none`, non-zero values and a declared resource breach cannot support `succeeded`. This checks the evidence at the storage boundary instead of trusting a worker-authored attribution field or a state label.

   Once validated, the store adds `attribution: workload` and the stable reason code `handler_exit_code_zero` to the terminal completion, and records a `succeeded` cause whose evidence retains the exact exit and resource counters. `job.reason` mirrors the stable reason for existing readers. The inputs are observed receipt fields; no text is guessed from the job state.

2. **Keep the receipt and terminal classification in one hash-bound result.** The `done` mapper reads attribution/reason from the canonical completion record and attaches them to the stream body. The ordinary job view, stream and result manifest all use the same receipt classifier, including for legacy rows read without replay. The result manifest copies those same values and the stored cause; its canonical bytes and digest therefore cover exactly what the stream reports. For stored successes that predate the validated receipt, readers use `unknown` plus `success_evidence_unavailable`; they do not turn legacy state into evidence.

   Alternatives considered: keep `done` metadata-free, which leaves the cross-service contract incomplete; synthesize a positive reason from `state=succeeded`, which has no causal evidence; or require new handler-authored text, which imposes a narrative API on every handler without improving the supervisor's execution evidence.

3. **Leave id and durable-state ownership unchanged.** The WorkloadStore transaction remains authoritative for job outcome and result bytes. The authority's intent ledger continues to own the stream sequence/status and references the same `intent_id`/job id mapping. The HTTP result route remains owner-scoped. No new database migration or independent outcome store is introduced.

## Risks / Trade-offs

- [Some historic or synthetic successful records lack the explicit resource counters] → expose `unknown` with `success_evidence_unavailable`; do not rewrite them or infer zero.
- [A completion near the record-size ceiling grows by a small amount when cause and reason are persisted] → re-encode under the existing record-byte bound and reject overflow before acknowledging the attempt.
- [Workers with incomplete resource receipts can no longer report success] → classify incomplete receipts as infrastructure in the worker and let the existing retry/lease policy resolve them; keep the store-side check as the authority boundary.
- [Manifest bytes change and thus the output digest changes] → compute the digest from the expanded canonical manifest and assert that stream reference and authenticated fetch agree in an integration test.

## Migration Plan

1. Land the additive result/stream/manifest contract and checks together. No schema migration is needed because job result, cause and reason are already stored fields.
2. New completion writes include validated metadata. Existing records remain readable; valid receipt-bearing records can be classified from their persisted receipt, while insufficient historic evidence remains explicitly unknown.
3. Rollback is code-only: the prior reader ignores the additive result keys. No stored rows need rewriting. Do not roll back the strict completion check separately from the stream/manifest reader.
