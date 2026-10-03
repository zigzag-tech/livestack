## 1. Remote provider admission and durable state

- [x] 1.1 Add operator-configured handler-to-GitHub-provider mapping, bounded runner resources, and a single provider slot; verify existing local submission bytes and admission tests remain unchanged and new tests refuse unconfigured handlers and exhausted slots; ledger each provider route, reservation, and refusal.
- [x] 1.2 Persist dispatch outbox, correlation, provider identity, GitHub run id/attempt, and reconciliation state with the workload attempt; verify mocked GitHub tests cover accepted dispatch, lost acknowledgement, authority restart, and no duplicate authorized run; ledger dispatch intent, reconciliation result, and run identity.

## 2. Remote authorization and lifecycle

- [x] 2.1 Verify GitHub OIDC signing keys and configured repository/workflow/source/actor/run claims, then cross-check the reported job id/status through GitHub's API before issuing a short-lived fenced grant; verify positive, manual-run, fork, wrong-source, replay, and stale-fence tests; ledger identity decision and grant expiry without raw tokens.
- [x] 2.2 Add remote heartbeat, cancellation, deadline, and terminal reconciliation while holding provider capacity through cleanup; verify restart, cancel, deadline, late heartbeat, and superseded completion tests; ledger each state transition and terminal reason.

## 3. Result verification and rollout

- [x] 3.1 Accept bounded remote logs and artifacts only through attempt-scoped transfer credentials and verify ownership, size, digest, source, run, and current fence; verify malformed, foreign, stale, oversized, and valid-result tests; ledger accepted and rejected artifact digests.
- [ ] 3.2 Update `_plans/durable-workloads.md` and the provider runbook; add fixed, bounded GitHub release routes to the existing HTTPS edge relay; configure the provider from the existing `gh` credential and relay key; then run the tagged GitHub-hosted macOS `--no-upload` proof. Verify unlisted routes and oversized control requests are refused, the edge key is not forwarded, no local handler runs, and authority restart preserves remote state; ledger placement, identity, and completion. Existing provider tests passed (312 passed, 31 skipped); live relay rollout and hosted proof remain pending.
