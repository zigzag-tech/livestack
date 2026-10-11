## 1. Least-privilege source grant

- [x] 1.1 Add fixed-owner `job_reader` principal validation and deny this role on every route except the new job-change reader; verify role/config and route tests cover valid configuration, invalid delegation/privileges, admin/caller refusal, and mutation refusal in `node-py/tests/test_workload_job_changes.py` and `node-py/tests/test_workload_principal_reload.py`. Ledger: none; this change performs read-only observation.

## 2. Bounded incremental API

- [x] 2.1 Add the owner-derived job-change route with no owner query, response owner metadata, strict query validation, tie-safe cursor, an effective-owner/timestamp index, and a batched attempt lookup; verify focused tests cover owner isolation, pagination/timestamp replay, query refusal, and independent 256-row/30-KiB bounds in `node-py/tests/test_workload_job_changes.py`. Ledger: none; this change performs no placement decision.
- [x] 2.2 Add `WorkloadClient.list_job_changes(expected_owner=...)` and validate owner metadata, rows, cursor, and response bounds; focused client tests reject malformed, mismatched-owner, and oversized responses in `node-py/tests/test_workload_job_changes.py`. Ledger: none; this change performs no placement decision.

## 3. Documentation and evidence

- [x] 3.1 Document dedicated `job_reader` provisioning, unique strong-token handling, least-privilege fields, and the loopback binding prerequisite in `node-py/docs/workload-job-source.md`; it does not recommend an admin bearer or alter operator configuration. Ledger: none; this change performs no placement decision.
- [x] 3.2 Run the focused job-change and principal-reload tests from `node-py/`: `rtk pytest -q tests/test_workload_job_changes.py tests/test_workload_principal_reload.py` — 44 passed. Ledger: none; this change performs no placement decision.
