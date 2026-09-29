# Worker tolerance of authority outages

A transient failure to reach the workload authority (connection refused/reset,
timeout, HTTP 502/503/504) must not stop a healthy attempt: the authority's own
lease (`lease_seconds`, 120 by default) already tolerates it. Implemented in
`livestack_node/workloads/lease.py` (`transient`, `retry_transient`,
`LeaseKeeper`) and `worker.py`.

- **Renewal**: `LeaseKeeper` retries every <=1 s until the deadline the
  authority last granted. It never extends the deadline locally; at the deadline
  the lease file is zeroed and the attempt stops with
  `LeaseExpired: no successful renewal ...` (fence still bites).
- **Status report** during a run (`status_report_seconds`, default 10): a
  transient failure is logged and skipped. This was the bug of 2026-09-29: the
  raw `URLError` from the report escaped the run loop and stopped the attempt
  (fence 1 -> 2).
- **Initial grant**: retried for `start_retry_seconds` (15).
- **Result handoff** (artifact upload, `worker/complete`): retried with backoff
  for `handoff_retry_seconds` (60, worker config) while the lease is not lost.
  Uploads are idempotent by digest. Exhaustion raises with the cause logged and
  the journal kept, so the next `reconcile()` replays it.
- **Not tolerated (unchanged)**: any authority answer 4xx (409 fence/cancel,
  404, ...) is `lease lost` and stops immediately.

Log lines: `authority unreachable, retrying (lease has N s left)` versus
`lease lost, stopping the attempt: ...`.

Tests: `tests/test_workload_worker.py` (`Outage` TCP hop: down/up on the same
port, reset first N uploads).
