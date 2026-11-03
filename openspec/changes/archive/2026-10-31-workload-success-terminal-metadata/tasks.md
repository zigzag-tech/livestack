## 1. Validate and persist successful completion evidence

- [x] 1.1 Validate `exit_code` and explicit zero resource counters before accepting `succeeded`; persist the bounded attribution, reason and typed cause evidence with the job result. Verify real SQLite completion tests cover valid, missing, malformed, non-zero and resource-breach receipts plus idempotent acknowledgement. Ledger obligation: no placement decision is made, so no decision-ledger record is added.
- [x] 1.2 Make the shared normal/recovery worker classifier treat incomplete success resource evidence as infrastructure instead of zero; verify worker tests exercise both receipt-backed success and missing counters. Ledger obligation: no placement decision is made, so no decision-ledger record is added.

## 2. Publish one durable outcome through stream and result fetch

- [x] 2.1 Map successful job result metadata to the `done` partial and preserve explicit unknown metadata for legacy successes without evidence; verify producer tests assert both shapes. Ledger obligation: no placement decision is made, so no decision-ledger record is added.
- [x] 2.2 Include the stored attribution, reason and typed cause evidence in the canonical owner-scoped result manifest; verify manifest tests assert the exact fields are covered by the returned bytes and digest. Ledger obligation: no placement decision is made, so no decision-ledger record is added.
- [x] 2.3 Extend the real WorkloadStore/stream/result-route integration fixture to complete a successful job from a supervisor receipt and prove ordinary job reads, the `done` partial and fetched manifest carry identical metadata under the published digest, including when a legacy row is read without replay; verify the focused integration test passes. Ledger obligation: no placement decision is made, so no decision-ledger record is added.

## 3. Contract and source validation

- [x] 3.1 Update the native stream producer documentation and durable-workloads design record to describe receipt-backed success and explicit legacy unknown; verify both descriptions match the contract. Ledger obligation: no placement decision is made, so no decision-ledger record is added.
- [x] 3.2 Validate the OpenSpec delta and run the focused store, worker, stream producer, result-manifest and authenticated-route tests; record the exact command, source tree and result in this change's evidence. Ledger obligation: no placement decision is made, so no decision-ledger record is added.
