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

## Facade runtime seam

The facade installs the route without installing a runtime. The service owner
must supply `app.state.pane_ask_runtime` as a `PaneAskRuntime`: a qualified
registry-derived engine adapter and asynchronous strict admission/finish
callbacks. Admission receives credential-derived owner/realm, profile, request
identity and deadline. It must reserve this exact engine and retain the lease
until finish; a warm engine does not excuse broker loss. The adapter checks the
granted unit/device/backend match. No legacy chat/admit routing is consulted.
Missing runtime, unqualified identity, broker outage and invalid output have
distinct named refusals.

`FleetExtractionAdmission` supplies strict callbacks using the existing
`/fleet/admit` and lease release APIs. It checks a real lease, unit, device,
host and registry-qualified target; an unexpected grant is released without
inference. Account/capacity refusal remains distinct from broker failure.
It records no local request history and never consults legacy `/admit`.

Production runtime installation remains pending. A fleet lease reserves a
node's concurrency, but does not load the model or bracket the serving facade's
local queue/busy state. Runtime installation must preserve those existing
controls, use genuine qualified metadata, and avoid a raw-engine call that can
be evicted while in flight. The current absent-runtime refusal stays in force.
