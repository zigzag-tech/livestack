## Context
The authenticated HTTP boundary resolves the principal and delegated owner. The store owns durable jobs, attempts and retention; workers own their authenticated lease observations. Existing job_changes uses owner-plus-cursor queries and capped response bytes. Source provenance is absent today.

## Goals / Non-Goals
Provide attributable observational joins while preserving existing behavior when omitted. Never accept a worker, explanation, observation timestamp or submitting principal from the provenance request. No prompt/content/credentials in metadata.

## Decisions
1. Use an optional versioned envelope with only kind and turn_id. Tokens are ASCII identifiers, each at most128 bytes, encoded envelope at most512 bytes. Unknown fields, invalid types, empty identities and reserved source labels fail closed. The principal must explicitly allow the requested kind; default grants allow none. Caller/admin submissions only; workers/readers cannot submit provenance. Existing delegated-owner checks still apply.
2. Stamp principal and resolved effective owner at the HTTP/store authority boundary. Caller payload/labels cannot override these. Store a nullable bounded column on the existing job, not a new unbounded ledger. Include provenance identity in idempotency compatibility; a different turn cannot silently relabel an existing job. Same turn retries retain identity. No implicit replacement jobs or retries on conflict.
3. Add source_turn_id to the fixed-owner reader query. Validate duplicates/unknown keys, use a composite owner/source-turn/update/id index, and fixed-count set-based queries. Preserve existing cursor semantics,256-row/30KiB caps. Emit source_kind/source_turn_id and current_explanation from persisted authority state, not labels. Explanation is a closed code plus observation time; no free-form request or worker text. Worker/attempt identities come only from authenticated attempts.
4. Add a provenance retention window, default24h and maximum7days. Expired metadata is cleared in the normal sweep in one bounded statement, including retained jobs; reads suppress expired provenance before the next sweep. Existing job-count/byte/retention limits continue to cap the containing records. No change to user job retention exemptions.
5. Backward-compatible nullable migration. New principal permission is opt-in. No configured credential copies, environment secrets or production probing. PR review and operator rollout precede live acceptance.

## Risks / Trade-offs
Cross-owner leakage → immutable server owner, fixed-owner read and cross-owner negative controls. Forged source → explicit producer grant plus closed envelope; raw tool input cannot become source. Retained records → independent metadata TTL. Idempotent reuse across turns → named conflict, never attach/relabel. Historical jobs remain source-unknown. No planner changes.

## Migration Plan
Ship additive authority support with grants off, then matching Jingway adapter, then Benchday consumption and an actual isolated turn/job/worker proof. Operators enable the intended source kind for the authenticated producer. Rollback disables the new grant; old calls remain compatible. Archive only after gates and runtime evidence.
