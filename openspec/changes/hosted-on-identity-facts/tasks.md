# Tasks

## 1. Harmony node facts

- [x] 1.1 Add a small Fact v1 helper with bounded identity validation and process generation/sequence. Verify with tests for invalid ids, TTL cap, and sequence growth. Ledger obligation: none, the helper is read-only.
- [x] 1.2 Add explicit node id and Benchday host configuration to the capability report. Verify with facade tests for configured and missing values. Ledger obligation: none, no placement decision occurs.
- [x] 1.3 Carry node Facts through the fleet view and add the authenticated complete identity snapshot endpoint. Verify with host broker and hostd tests for full-cut replacement metadata and unauthorized refusal. Ledger obligation: none, no planner action occurs.

## 2. Workload worker facts

- [x] 2.1 Add an optional Benchday host mapping to worker principals and include it in immutable binding checks. Verify principal loading/reload tests. Ledger obligation: none, principal validation does not place work.
- [x] 2.2 Add a bounded current-worker identity query and authenticated workload identity endpoint. Verify tests for registered, stale, and unmapped workers and one-query bounded retrieval. Ledger obligation: none, endpoint reads only.
- [x] 2.3 Reject worker report fields that try to override the principal-owned Benchday host mapping. Verify registration schema tests. Ledger obligation: none, a refused report creates no placement decision.

## 3. Verification and landing

- [x] 3.1 Run the focused Livestack Python tests and relevant Hub/hostd tests on a supported host. Ledger obligation: inspect no new placement records from identity-only reads.
- [x] 3.2 Run the required repository test gate; document every red failure with a falsifiable reason. Ledger obligation: verify the source writes no new decision records during tests.
- [ ] 3.3 Land the source work as an isolated commit through the repository's reviewed landing path. Ledger obligation: no runtime decision is created by landing.


### Verification record

- Focused run on zz-tower2: pytest -q tests/test_identity_facts.py tests/test_workload_identity_facts.py tests/test_capability_load.py — 32 passed, 1 warning.
- Full pytest -q was attempted. Collection reported six errors: test_imagegen_runtime.py lacks Pillow (PIL); test_sam21_adapter.py lacks NumPy; and test_mesh_e2e.py, test_mesh_peer.py, test_mixed_roster.py, and test_relay_rotation.py raise module-level pytest.skip during collection in this environment. These are environment/optional dependency collection failures, not assertions from the identity-facts tests.
- Benchday focused stream/fact batch on zz-joe: 43 tests passed; fleet_roster_consumers.test.ts did not start because the linked Jingway checkout is missing its runtime package ai (imported from src/server/LLMConfig.ts). This is an environment dependency failure, not a failed assertion.
- The identity endpoints only construct authenticated read snapshots from current broker/worker state; they do not call placement or ledger decision writers.
