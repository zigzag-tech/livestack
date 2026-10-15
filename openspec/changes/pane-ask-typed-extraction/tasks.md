- [x] 1. Synchronize extraction schema and semantic checks; verify contract tests and byte parity. Ledger: no inference admitted by validation.
- [x] 2. Resolve extraction profile and reject unqualified admission and retired adapters; verify refusal before model calls. Ledger: named refusal only.
- [ ] 3. Implement bounded nominal-27B extraction adapter and authenticated facade integration; verify real HTTP, deadline, grounding, identity and outage controls. Ledger: existing inference/admission outcomes.
- [ ] 4. Produce genuine qualification evidence and matching registry metadata; refuse unavailable evidence rather than fabricate it. Ledger: qualification receipt and measured profile identity.
- [ ] 5. Land, obtain owner-operated guarded publish and verify live typed extraction; archive this worker change after proof. Ledger: deployment and readback evidence.

Verification: 19 JSON contract files are byte-identical to the consumer copy.
The additive contract revision is v1.1.0; HASH binds sorted relative JSON paths,
NUL delimiters and exact bytes (recipe in HASH.md). Contract/ingress controls
use a task-private HTTPX 0.27.2 environment compatible with the installed
Starlette test client. These controls do not constitute model qualification.
The foundation's 34 contract/ingress checks pass, including the content receipt,
authenticated refusal before admission and both retired-adapter refusal paths.
Consumer contract and extraction-client controls pass (9 checks).

Adapter/facade unit: nominal_27b_adapter.py implements a bounded, already-admitted
27B engine call, source-quote grounding and exact request/evidence identities.
pane_ask_ingress.py registers the authenticated /v1/decisions facade route;
it requires fresh admission for warm and cold work and closes admitted leases
on both success and engine failure. No request history is stored. Its runtime
is deliberately absent by default, producing profile_unavailable until the
service owner installs genuine qualified registry metadata and strict broker
callbacks. This is not a live serving deployment or a model qualification.
52 contract/HTTP/ingress controls pass. The absolute engine deadline explicitly
cancels and collects its task, preserving a typed timeout despite HTTPX/AnyIO
cancellation cleanup. Task 3 remains open for actual registry/broker wiring.
The nine existing serving-facade routing checks skip locally because the
compiled shared_py residency planner is absent; they are not counted as passes.
Do not build that native dependency on the shared tower to clear this limitation.

Strict fleet callback unit: fleet_extraction_admission.py implements the current
fleet lease wire API with no degrade-to-grant behavior, retries or request store.
Nine real HTTP controls pass for authenticated scope, exact target, real lease,
quota/refusal, malformed/oversized receipts and release acknowledgement.
Production callback installation, local queue/busy integration and real broker
execution remain open under task 3; wire controls are not residency proof.

Resident-scope integration: the production facade now installs protection that
uses its existing unit queue, busy counter and managed-unit busy flag. The
extraction ingress refuses a missing guard before admission and closes a fleet
lease when a cold/saturated guard refuses. Thirty adapter/ingress/fleet HTTP
controls pass, including actual queue/busy observations during generation and
cleanup after malformed engine output. Two immediate-capacity/FIFO controls
pass. The nine routing checks previously skipped now run with the installed
Harmony runtime's native shared_py planner; all 20 routing/queue checks pass
against this worktree with the test broker isolated. This supersedes the local
missing-native-dependency limitation; no model qualification or deployment is
inferred. Actual qualified runtime installation remains pending under task 3.
The combined contract, adapter, ingress, fleet, native-facade routing and queue
selection passes all 84 checks in the installed Harmony runtime against the
isolated task worktree. This includes no GPU model execution and does not
discharge the model qualification or live-serving requirements.

Execution-accounting correction supersedes the earlier cancel-the-HTTP-task
deadline implementation: expiry/cancellation now collect engine completion
while retaining local capacity, busy state and the fleet lease. Twenty-two
adapter/ingress controls pass, and two additional real HTTP lifecycle controls
observe all reservations held while the backend remains active and released
only after it completes. These controls use a test HTTP backend, not a GPU
qualification. Production enablement still requires finite execution and
confirmed termination under transport failure/stuck execution; no development
abort API was enabled or used. Task 3/4 remain incomplete for those requirements.

Development-source verification: the remote extractor dev file exists, is
320,339 bytes and contains 32 rows, with SHA-256
0c1fa70e360726628432f98eba2b2b8c78e86300a141237d49ab10d7df49c0eb,
matching Benchday's committed pane-question baseline. Only metadata/schema and
kind counts were inspected via read-only SSH after the file RPC connection
closed twice. No transcript text was copied into this repository and the final
surface was not opened. The metadata-only manifest is
node-py/docs/decisions/pane-ask-development-source.json. This surface lacks
input-kind and negative examples and cannot independently qualify the profile.
The 67-row surface in the baseline belongs to admission classification, not
extraction. ASR documentation still names zero usable held-out contexts; no
new genuine file-filter capture or qualification receipt has been found.

Operator delegated worker deployment and qualification on 2026-10-14. The task
checkout was restored from its retained branch and advanced to current origin.
Installed vLLM source inspection found a supported worker-extension/collective
RPC seam, with abort requests retained in the ordered input queue. Candidate
`vllm_execution_barrier.py` supplies a correlated CUDA synchronization receipt
after abort, and refuses async/deferred batches and all parallel layouts. Four
receipt/configuration vectors pass, including a deliberately uncorrelated
receipt and refusal before any work for unsupported configurations. The module
retains no request/response store. These are source controls, not actual GPU
termination evidence. Protected engine execution control, actual synchronous
configuration qualification, runtime installation and guarded rollout remain
required under tasks 3–5. No development abort API or unbounded Responses store
was enabled. The genuine 32-row development surface is privately captured with
its recorded SHA; the sealed final surface remains unopened.

Protected execution source subsequently landed in `88cc5ad6`, `76389598`,
`ed9c102a` and `da2a5ef2`: transport loss retains all reservations, frontend
cancellation precedes the supported GPU barrier, the opt-in private endpoint
enforces authentication/loopback/body/deadline policy, and the facade verifies
correlated completion/stop receipts. Thirty-five targeted HTTP/facade controls
pass. Those controls are not actual native GPU cessation or quality evidence.

The production startup installer now verifies the local host/device/unit and
immutable registry profile before loading credentials or creating clients.
The committed unqualified profile is refused explicitly, even with a matching
artifact digest. The facade reports the named failed prerequisite phase.
Nineteen installer/ingress controls pass; twenty native facade-routing/queue
controls pass in the installed Harmony Python runtime with an isolated broker
address. OpenSpec validation and source compilation pass. Task 3 remains open
for a genuinely qualified profile, actual configuration installation, and
successful native engine/broker readback. Tasks 4–5 remain unproven. No runtime
has been enabled by these source controls or by source/inventory counts.

Live execution controls (2026-10-15): the operator explicitly authorized direct
resident-service restarts. The service's Python environment now registers the
opt-in endpoint plugin, and its private loopback credential/configuration and
synchronous single-worker extension are installed. No Responses store was
enabled. Startup exposed a real alias bug: ModelConfig retains only the first
served name. Fix `36d228a0` uses the native base-model registry, which accepts
the configured base aliases and rejects LoRA/unknown names. Diagnostic unit
`5535d54c` names the fixed prerequisite phase without logging request data or
credentials. Twelve targeted endpoint/controller controls pass.

The deployed private control rejects unauthenticated input with 401 and
authenticated malformed input with 400. A real native GPU completion returned
200 in 467 ms with correlated completion confirmation. A 1,500 ms deadline
returned 504 in 1,515 ms, with execution started and cessation confirmed through
the supported CUDA barrier. The private control receipt is 1,881 bytes, SHA-256
`43c1b4b7ee5f47dd8300612128b628e99fd8bc021376fa85a45c5800352e6d1e`.
Both model and facade health returned 200 afterwards; the public decisions
route rejected unauthenticated input with 403. Its qualified runtime remains
disabled. These controls establish native completion/deadline behavior, not
full transport-loss/broker qualification or model quality.

The genuine development surface was then measured once using its recorded
digest, final assistant messages, and the production extraction instructions.
All 32 gold quotes were present in those messages. Results: 27 native completions
and five deadline refusals, all five with cessation confirmed; 26 schema-valid
outputs; 24 grounded asks; 22 strict grounded/kind-correct/stopped completions
within eight seconds. One output violated the quote-length bound. Development
receipt: 64,642 bytes, SHA-256
`629e546eef33dfc5769ea128cbf2961724d08e0cb8bab3e89cf372f9ee1d8bb9`.
The model revision/tokenizer identity are not yet verified, the surface lacks
negative/input-kind coverage, and the sealed final remains unopened. This
does not qualify the profile or authorize promotion. Tasks 3–5 remain open.

A development-only prompt candidate (`311b114b`, instruction SHA-256
`485139449baeaf25ac2bf9d331f84a05e119e2f438db78a93aff502c61c17524`)
clarified kinds and requested shorter output. On the same 32 source rows and
eight-second budget it produced only 12 strict successes, 14 schema-valid
grounded asks and 16 deadline refusals, all with confirmed cessation. Receipt:
43,196 bytes, SHA-256
`c70250e24eecc3effd11dc9a87362aca4344d10794109e918155280db446156d`.
A subsequent native metrics read observed four other running requests and five
queued requests; load was not controlled, so the difference is not attributed
solely to the prompt. The evidence does not support adoption. Forward revert
`612eb49f` restores the preceding instructions; the candidate was never enabled
through a qualified public runtime. Both private measurements remain retained,
and the sealed final is still unopened.

Identity preparation (2026-10-15): the resident engine was restarted with both
`--revision` and `--tokenizer-revision` pinned to
`1f05c441c4e64ae0549de44fa9ea5a6d43610314`; the actual serving process arguments
were inspected after startup. The tokenizer bundle consists of the sorted
relative names `chat_template.jinja`, `tokenizer.json`, `tokenizer_config.json`.
Its hash is SHA-256 over each UTF-8 filename, NUL, exact bytes, NUL in that
order: `09281a005af04302491caa1b7c9f8b2599ce3ecf72ed0af97cfc65c0f9ed25fa`.
Three native `/tokenize` controls exactly match token IDs from those bytes.
The pinned model's native completion returned 200 in 273 ms; the 1,500 ms
deadline returned 504 in 1,533 ms with confirmed cessation.

Actual client-disconnect control did NOT pass: ingress held capacity immediately
after the socket closed, but the follow-up completion after the collection
window returned 429 because the prior execution still owned its slot. The log
names an unconfirmed GPU stop. The controller remains quarantined, preserving
the reservation instead of accepting more work without cessation evidence.
Identity/control receipt: 3,836 bytes, SHA-256
`10ec432bba43702baca6fd4e46e2cc1da37122fbff4defb7aa26b0baf3b83a4d`.
The failed receipt is retained. A fixed-phase diagnostic now distinguishes
engine-abort failure from GPU synchronization/receipt failure without logging
exception payloads. Full transport-loss/broker qualification and quality remain
unproven; no qualified public runtime or final-corpus evaluation is enabled.
