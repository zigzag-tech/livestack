## Context

See `proposal.md` for motivation. The service starts one `WorkloadAuthority` for the realm workload contract. `IntentLedger` persists the authenticated owner, stream intent ID, request body, current status and sequence before backend dispatch. `WorkloadStore` independently owns durable job and attempt state; its idempotency key is set to the stream intent ID. `BlobStore` owns the caller's CAS namespace and both the referenced request and admitted input objects are read under that same owner.

The production service already passes `server.blobs` to `start_for_workload_authority`, which selects the default bounded JSON resolver. The stale `HARMONY.md` gap statement predates that wiring. The missing proof is an integration test that crosses framed ingress and the production startup factory into real WorkloadStore/BlobStore admission.

## Goals / Non-Goals

**Goals:**

- Prove owner identity, reference verification, strict handler/input admission, request-key binding and outcome attribution through the same boundary used by the service.
- Prove rejected intents do not create WorkloadStore jobs or attempts and that same-owner replay does not dispatch twice.
- Keep the test isolated in temporary state with fake framed ingress and no reachable production authority.

**Non-Goals:**

- Change the current workload admission policy, authentication source, service activation flag or deployment state.
- Replace `ScriptedBackend` coverage for authority ladder transitions; the new test covers the real-store boundary that those tests cannot prove.
- Change HTTP job APIs or WorkloadStore's durable job model.

## Decisions

### Exercise the production factory with real stores

The integration test will build a temporary `WorkloadStore`, configured principals/handlers and `BlobStore`, start the workload stream runtime with the default resolver (no injected `resolve_spec`), and write through `FakeIngress` using real framed messages and authenticated principals. This verifies the service wiring, default resolver, store admission and authority publication together.

The alternative—testing `_workload_spec_resolver` alone or using `ScriptedBackend`—would prove local parsing but could not catch missing service wiring, owner mismatch at the stream boundary, duplicate job creation or ignored WorkloadStore policy. Existing scripted tests remain for transition cases that are not represented by a real store lifecycle.

### Keep identity across the two durable owners

The authority ledger uses the stream `intent_id` as its principal-scoped intent identity. `StoreBackend` overwrites any request key in the referenced JSON with that same `intent_id` before calling WorkloadStore. WorkloadStore remains the owner of job/attempt IDs and admission state; the authority ledger stores the resulting job ID and links it to the intent. The test will assert both the linked job count and the exact key, including after replay.

### Cover refusals without weakening admission

Valid and invalid cases will use the real input CAS path and real configured handler allowlist. Tests will include foreign-owner and malformed request objects, digest/size/media mismatch, missing/oversized references, an uninstalled handler, and invalid input object. Backend failure and cancellation are separate outcomes. Assertions inspect both the returned/published terminal partial (status, attribution and bounded reason) and durable job/attempt counts so no refusal can pass merely by returning a convincing partial.

The test harness uses temporary directories and a fake local ingress socket. It never starts `workloads.service.main()`, opens a production listener or contacts a production worker. It calls the same `start_for_workload_authority` factory that `main()` calls after store recovery.

## Risks / Trade-offs

- [A full WorkloadStore integration fixture can be slower and more coupled to store setup] → Reuse the existing lightweight fixture helpers and keep the case set in one isolated test class; run the focused Python test before the admitted check.
- [A failure in the factory may reveal a real configuration gap] → Fix the source-wired default resolver or test fixture based on the failing boundary; do not inject a resolver just to make the test pass.
- [Cancellation races with placement] → Assert the contract's pre-placement cancellation rule and allow only the authority's recorded terminal outcome; do not claim cancellation wins after placement.

## Migration Plan

No service restart or production config change is part of this work. Update `HARMONY.md` and `_plans/durable-workloads.md` to state the source-verified resolver behavior and remaining operational boundaries. If the integration test disproves the current wiring, land the minimal code repair with its test; otherwise ship only the proof/spec/reference correction. Rollback is a code revert and does not affect already persisted job or intent state.
