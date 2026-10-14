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
