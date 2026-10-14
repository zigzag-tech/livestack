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
node's concurrency but does not load the model. `ResidentExtractionScope`,
installed by the facade, borrows immediate capacity from its existing unit
queue and brackets inference with the existing busy counter and managed-unit
busy flag. Missing protection is refused before admission. Saturation refuses
without a stranded blocking waiter; cold/unready engines are named unavailable
without starting a model load. Capacity, busy state and the fleet lease are
released on failed output as well as success. Existing FIFO waiters retain
priority. Runtime installation still requires genuine qualified registry
metadata. The current absent-runtime refusal stays in force.

## Execution completion and caller expiry

The caller deadline controls publication, not the lifetime of admitted GPU
work. Cancelling an HTTP task cannot by itself acknowledge GPU termination.
The adapter now leaves the engine connection open and collects its non-streaming
completion before leaving the resident scope, even after caller expiry or
cancellation. Repeated caller cancellation cannot cancel that collection.
The expired/cancelled result is discarded and the outer scope/fleet cleanup
then releases the reservation. The host's existing eight-second fetch deadline
continues to bound its demand-publication wait.

Before runtime installation, backend qualification still needs to prove a
finite execution lifetime and confirmation of termination on transport failure
or stuck execution. The installed vLLM's development-only abort route is not
enabled by this change and is not accepted as termination proof. No backend is
qualified by the HTTP fixture's completion signal.

## Production cessation candidate

The installed vLLM source supports `--worker-extension-cls` and collective RPC.
`vllm_execution_barrier.py` supplies a storage-free extension that synchronizes
its actual CUDA device and returns a correlated nonce. Its frontend helper
aborts the tracked extraction first, then obtains the worker receipt on the
same ordered single-engine IPC channel. Async scheduling, deferred batches,
data/pipeline/tensor parallelism, a missing extension and any invalid or missing
receipt refuse. The two-second RPC bound names unconfirmed cessation; it never
allows release on timeout. This is candidate support, not a GPU qualification.

The restrictions matter: a deferred batch could launch work after a bare CUDA
barrier, and an HTTP cancellation only proves a closed socket. Installation
still needs a protected facade-to-engine execution-control surface, a qualified
synchronous launch configuration and actual GPU outage/cancellation evidence.
No vLLM development route or unbounded Responses API store is enabled.

Transport failure and an oversized response before completion now propagate an
explicit `ExecutionUnconfirmed` marker. The resident scope retains the actual
queue slot and busy accounting; the ingress retains the fleet grant. A later
request cannot redispatch into the occupied local slot. This uses only the
existing bounded capacity counters, not a growing quarantine history. A real
accepted HTTP disconnect control verifies all three reservations and refusal
of redispatch. Complete malformed output still releases normally. Recovery and
bounded cessation remain pending the protected engine control and genuine GPU
qualification; this cleanup repair does not enable runtime installation.

The installed vLLM API server exposes the opt-in `vllm.endpoint_plugins` seam:
its `init_state` receives the same engine client as the serving handlers.
`TrackedExtractionExecution` supplies the endpoint's bounded frontend owner.
It refuses unsupported launch configurations before rendering, owns at most
one task, and cancels/collects the renderer before aborting and synchronizing
the GPU. Renderer collection is bounded at two seconds and the GPU receipt at
two seconds. An uncollected renderer or invalid GPU receipt retains that one
slot and refuses redispatch. No receipt/request history is appended. The
endpoint must still authenticate and cap its body at 64 KiB, supply exactly the
same tracked request identity to the chat renderer, and wire the control to the
facade. Adversarial task controls are not real vLLM execution qualification.

`vllm_extraction_endpoint.py` registers an opt-in endpoint plugin named
`harmony_extraction_execution`. The installed vLLM loader requires an explicit
plugin allowlist; packaging does not activate it. Initialization reads an owned
private configuration at `/etc/harmony/extraction-execution.json` (4 KiB), with
exact keys `version: 1`, `tokenFile` and `model`. The credential file is at most
256 bytes and private; configuration symlinks and public permissions refuse.
The model must be an engine base alias, not a personal LoRA alias. Missing
configuration or unsupported engine ordering leaves the private route unavailable.

The private `/harmony/extraction` route requires loopback, its credential and a
32-hex caller nonce before reading a 64 KiB body. A single ingress lock bounds
simultaneous body buffering to one request. It enforces the exact fixed generation
policy, and bounds body-plus-generation publication to eight seconds. It calls
the native chat handler without the raw HTTP request, so client request-id
headers cannot override the tracked engine identity. Correlated completion or
confirmed stop is reported; an uncertain stop keeps the single execution slot.
No Responses API store, model promotion or public runtime installation occurs.
Facade transport wiring, successful native completion, real GPU termination
and source-quality qualification remain required before guarded rollout.

`ProtectedExecutionTransport` connects the facade adapter to that private
endpoint. It permits only loopback HTTP, no credential-bearing URL, and the
exact execution route. Credentials remain in a client initialized from private
settings. Every call carries a fresh nonce and the caller's absolute deadline.
One response is capped at 64 KiB and the transport wait at fourteen seconds
(eight for execution, two for renderer collection, two for the GPU receipt,
with transport allowance). Missing, malformed or mismatched completion/stop
receipts propagate `ExecutionUnconfirmed`, retaining all reservations. A
correlated stop or explicit non-execution propagates the typed engine failure
and allows normal cleanup. Real HTTP facade controls cover completion, confirmed
stop and uncertain-stop cleanup through the actual queue and broker callback.
These are protocol controls, not actual GPU or model-quality qualification.

The serving facade's startup hook now reads an optional private runtime
configuration at `/etc/harmony/pane-ask-runtime.json` (8 KiB). It binds the exact
local host, CUDA device and managed unit, checks the immutable profile bytes
against their registry digest and qualification hash, and refuses the committed
`:unqualified` profile before loading credentials. Matching physical-kind
membership is mandatory. No source count or declared engine name promotes a
profile. Qualified rollout must update the consumer/worker profile pin together
using the actual measured qualification identity.

Only after those checks does it create one protected-engine client and one
strict-fleet client, with connection limits of one and two respectively. Startup
owns at most that pair; shutdown closes both and disables the runtime. A refused
prerequisite is recorded by a named phase and reaches callers as
`profile_unavailable`; credentials and configuration values are never logged.
The real shipped-profile refusal controls pass. No genuine qualified profile
exists yet, so successful native installation/readback remains unproven.
