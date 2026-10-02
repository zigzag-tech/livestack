# Tasks

Ledger obligation: none. Connection reuse is not a placement or routing
decision; no ledger record is added or removed.

## 1. Transport

- [x] 1.1 `transport.KeptConnection` (persistent HTTP/1.1, one retry on a reused connection, urllib error surface); tests: `tests/test_workload_keepalive.py` (real authority server, real sockets).

## 2. Worker

- [x] 2.1 `WorkloadClient` rides a kept connection, `channel()` for the lease keeper, proxy fallback; tests: `test_workload_keepalive.py::test_one_connection_carries_many_renewals`, `::test_renewal_reconnects_after_server_drops_connection`, `::test_renewal_survives_blocked_new_connects` (fails on the per-request client).
- [x] 2.2 `LeaseKeeper` renews on its own channel and closes it; tests: as 2.1 plus existing `test_workload_*` suites.

## 3. Authority

- [x] 3.1 Keep-alive responses with `Content-Length` when the request body was consumed; close otherwise; tests: `::test_error_response_has_content_length_and_unread_body_closes`.
- [x] 3.2 Bound = 32 + 2 x worker principals, idle eviction at the bound, idle bound named; tests: `::test_bound_follows_worker_principals`, `::test_idle_kept_connections_cannot_starve_a_new_request`, `test_workload_network.py::test_connection_dropped_at_bound_is_logged`.
- [x] 3.3 Compatibility: new client vs closing server, urllib client vs keep-alive server; tests: `::test_client_against_closing_server`, existing `test_workload_http.py`.

## 4. Rollout

- [ ] 4.1 Authority and every worker on a release containing this change (order does not matter: each side is compatible with the other's old version); archive this change once rolled out.
