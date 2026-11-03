## 1. Real authority integration fixture

- [x] 1.1 Add a temporary real-WorkloadStore/BlobStore harness that starts `start_for_workload_authority` with its default resolver and sends real framed writes through `FakeIngress`; verify it with `python3 -m unittest discover -s node-py/tests -p 'test_streams_workload_authority_integration.py' -v`. Ledger obligation: expose and inspect both the durable `IntentLedger` record and WorkloadStore decision ledger without writing outside the temporary fixture directory.

## 2. Owner-scoped reference admission

- [x] 2.1 Cover a valid CAS request and same-owner replay through framed ingress; assert one admitted job, `job.spec.key == intent_id`, current authority partial replay and one durable admission/placement decision. Verify with the focused integration test. Ledger obligation: prove the intent row links to exactly one WorkloadStore job and replay creates no second decision or attempt.
- [x] 2.2 Cover missing, foreign-owner, wrong-media, wrong-size, digest-mismatch, invalid-UTF-8, non-object and over-one-MiB request objects plus an uninstalled handler and invalid input-object reference. Verify every terminal stream refusal's attribution/reason and the real store's job/attempt counts. Ledger obligation: refusals create no placement/attempt record and cannot overwrite another owner's identity.

## 3. Terminal outcomes

- [x] 3.1 Cover WorkloadStore unavailability, requester cancellation before placement, repeated cancellation and unauthorized cancellation through framed ingress; verify authority-published terminal partials and bounded reasons. Ledger obligation: each accepted or refused intent's final durable record matches its terminal partial and preserves the principal and job link.

## 4. Operator reference and validation

- [x] 4.1 Replace the stale resolver-gap note with the source-proven wiring and remaining operational boundaries in `HARMONY.md` and `_plans/durable-workloads.md`; verify with `openspec validate --strict` and the focused integration test. Ledger obligation: document that WorkloadStore remains the decision-ledger owner and that no production decision, config or job state was changed by this fixture.
