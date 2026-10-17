# Tasks

## 1. Shared bounded paging

- [x] 1.1 Add a pure identity-cut page packer with the 4,096-Fact, 2 MiB canonical JSON, 24 KiB complete-partial, and 128-page bounds; produce one page for an empty cut.
- [x] 1.2 Reject invalid or over-bound complete cuts before publishing any page; preserve one generation/sequence/observed time across all pages and ensure older trailing page keys are ignored by the new page count and expire under the stream TTL.
- [x] 1.3 Add focused helper tests for exact limits, multibyte UTF-8 accounting, oversize refusal, empty cuts, page ordering, fence consistency, and stale trailing pages.

## 2. Fleet broker publisher

- [x] 2.1 Create the fleet `IdentitySnapshotPublisher` from its explicit authority ID and pass it to the stream publisher.
- [x] 2.2 Register `livestack.identity-facts.fleet/1` only on the observe-only fleet broker; keep host brokers from competing for its one-per-realm authority.
- [x] 2.3 Refresh complete fleet cuts every 15 seconds and preserve named unavailable, authority-refused, source-read, bound, and ingress outcomes.
- [x] 2.4 Add positive and refusal tests for the fleet source and assert identity reads do not write a placement/decision record.

## 3. Workload authority publisher

- [x] 3.1 Pass the workload server's existing `identity_facts` source to its native stream runtime and share its publisher; remove the hostname fallback for missing `identity_authority_id`.
- [x] 3.2 Register `livestack.identity-facts.workload/1` only from the workload authority and refresh a complete cut every 15 seconds.
- [x] 3.3 Add source tests for principal-owned worker mapping, empty cuts, ambiguous/unmapped worker omission, explicit authority configuration, refusal, and no decision-ledger writes.

## 4. Cutover and landing

- [x] 4.1 Remove the two HTTP routes and their tests after confirming the Hub is the only caller; retain no HTTP fallback in either repository.
- [ ] 4.2 Run focused source tests and the coordinated isolated `streams.hosted-identity-facts` assertion after the Benchday contract and authority fixture land.
- [ ] 4.3 Record final-tree source identity and admitted results; land this change with receipts and archive `identity-facts-source-streams`.

Every test task's ledger obligation is zero placement/decision records from identity-only reads.
