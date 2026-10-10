## 1. Broker telemetry

- [x] 1.1 Add bounded per-kind 60×60-second counter rings and a 256-sample latency reservoir; record caller grants, explicit lease outcomes, and successful evictions. Test bucket expiry, unknown outcomes, percentiles, and sample/key bounds; assert the existing placement and outcome ledger records are unchanged. Verified in `node-py/tests/test_hostbroker.py`: rolling expiry, p50/p95, reservoir/key/count bounds, explicit versus unknown outcomes, successful and failed eviction dispatches, and no status-generated ledger records.

## 2. Status projection

- [x] 2.1 Add `/status.counters` with the counters and queue-depth gauges derived from the peer snapshots already read by `/status`. Test known zero, missing/malformed queue state, failed peer refresh, and that status performs no extra peer requests; retain the existing peer and ledger response behavior. Verified in `node-py/tests/test_hostd_counters.py`; each peer refresh is called once and partial queue totals are omitted.

## 3. Verification

- [x] 3.1 Run the focused hostbroker and hostd tests plus strict OpenSpec validation; confirm the counters response is bounded and all reported values have observable test evidence without adding ledger events. `python3 -m pytest -q tests/test_hostbroker.py tests/test_hostd_counters.py tests/test_hostd_lease_routes.py tests/test_fleet_admit.py`: 103 passed. `openspec validate host-unit-outcome-telemetry --strict`: valid. `git diff --check`: clean.
