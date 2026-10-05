## 1. Authority

- [x] 1.1 `upload_grants` principal flag (caller/admin only, default false) and `upload_grants.py` (tables `upload_grants`, `upload_grant_events`, caps 4096/512/8192, 24 h terminal collection); tests: `test_workload_upload_grants.py::test_capacity_bounds_and_event_rotation`, `::test_principal_flag_validation` (real SQLite).
- [x] 1.2 Mint route bound to the caller's own namespace, exact digest/size/expiry, arbitrary-owner and changed-binding refusal; tests: `::test_issue_authorization_and_validation`.
- [x] 1.3 Capability-only `PUT` route streaming through `BlobStore.put` with exact `Content-Length`, per-grant in-process lock, no other route reachable; tests: `::test_grant_round_trip_stores_in_owner_namespace_and_cannot_do_more`, `::test_capability_is_bound_to_digest_and_exact_size_and_failed_uploads_leave_nothing`, `::test_concurrent_upload_is_refused_in_use` (real HTTP).
- [x] 1.4 Owner status by request id, reconciled from the CAS owner/digest/size binding; rotation, revocation, expiry outcomes and audit events without capability material; tests: `::test_lost_reply_reconciles_from_cas_without_second_transfer`, `::test_expiry_rotation_revocation_and_durability`.

## 2. Rollout

- [x] 2.1 Update `_plans/durable-workloads.md`; land on origin/main.
- [ ] 2.2 Deploy to the Harmony authority and enable `upload_grants` only for the ZZOPS dispatcher principal; verify a real grant, PUT and status round trip.
