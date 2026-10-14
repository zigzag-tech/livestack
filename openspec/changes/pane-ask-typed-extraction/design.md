## Context

The consumer already emits `benchday.decision.v1` with one `ask` extraction
question and the pinned `pane-ask-27b-v1:unqualified` profile. Source metadata
is hashed by the consumer. Request and result bodies are capped at 64 KiB.
The nominal model tier is [26,28) billion parameters; qualification remains
absent until separately recorded evidence exists.

## Decisions

Reuse the existing profile and the consumer's schema bytes, synchronized at
the additive contract revision `typed-decision-contract-v1.1.0`. Contract validation
admits its envelope while admission refuses the unqualified profile before GPU
work. A separate extraction adapter will consume only qualified registry
metadata, enforce the nominal tier, thinking/tools off, temperature zero and
1024 output tokens, and validate the source-bound answer. Retired Laya adapters
must refuse extraction explicitly; classification behavior is retained.

The facade owns authentication, residency admission and bounded request lifetime.
The caller owns request/evidence identities; the worker echoes them exactly and
cannot claim input-submission authority. Profile/qualification files remain
operator-owned durable state. This change stores no per-request history.
Failure remains a typed unavailable/invalid/deadline result, not a successful
empty answer. Genuine `no_grounded_ask` is a distinct successful extraction.

## Rollout

Land the contract/admission unit first. Add the adapter and route only behind
qualified profile admission, verify boundaries with HTTP positive and refusal
controls, then obtain the service owner's guarded deployment and live readback.
Plumbing tests do not constitute model quality qualification or enablement.
