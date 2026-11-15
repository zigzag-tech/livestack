## Contract projection

- [x] 1.1 Align contract identity, pair key and 32-pair configuration refusal behavior.
- [x] 1.2 Emit full bounded schema-valid bodies with typed preferences and content-sensitive decision IDs.
- [x] 1.3 Distinguish bodyless no-target absence from validation/computation failures.

## Authenticated producer and integration

- [x] Separate ranking input attachment from fleet publication; unchanged rank-only startup does not register the daemon-owned fleet contract. Regular SDK/startup controls pass; real daemon qualification remains task 2.1.

- [x] 2.1 Verify native ingress registration/refusal, reconnect and failure recovery on the exact source.
- [x] 2.2 Verify actual-ranker positives, schema/size/configuration negative controls and same-second IDs.
- [x] 2.3 Hand off the exact source pin for Benchday's native fleet fixture and legacy poller/frame removal.
- [ ] 2.4 Record final-tree receipts, integrate source through the repository's normal gate, sync and archive.

      Current-main integration candidate (2026-11-14): merge origin/main
      b46ec73d with the existing native candidate, preserving both histories.
      All65 stream controls and strict change validation pass; real authority,
      final matching-consumer qualification and source landing remain open.
      Evidence: `evidence/current-main-integration-20261114.md`.

Verified source phase2026-11-15: the exact8bfef6bb4c1745a2561a184b7b5050e5dddd8b29
closure has passing real daemon registration, refusal, failure/absence,
reconnect and fresh-owner/account-isolation receipts. The combined daemon also
passes actual32-pair normal runtime startup and automatic socket recovery.
See `evidence/native-source-handoff-20261115.md`. Final combined Benchday realm
qualification and both repository landings remain open in2.4. No production
configuration or service activation is part of this phase close.
