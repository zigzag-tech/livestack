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
