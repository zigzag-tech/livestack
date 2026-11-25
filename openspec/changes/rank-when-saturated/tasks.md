## 1. Trigger

- [x] 1.1 `_local_ready` requires a free slot; `_local_saturated` names the case; both call
      sites pass `saturated=`. Test: `tests/test_unit_queue.py::test_a_full_resident_unit_asks_the_fleet_and_an_idle_copy_wins`
      (fails on the previous code). Ledger: route record `trigger: saturated`.
- [x] 1.2 `wants_fleet_route(..., saturated=)` returns `saturated`, never for a routed
      request. Test: `tests/test_harmony_llm_regional_routing.py::test_wants_fleet_route_names_its_trigger`.

## 2. Ranking

- [x] 2.1 `fleet_rank.rank` prices every target when any warm one is full, and orders by
      finish. Tests: `tests/test_cold_warm_tradeoff.py` (five saturation cases, two of which fail
      on the previous code). Ledger: `finish_s` on each target row.
- [x] 2.2 `_unit_evidence` returns per-unit inventory without `prefer`; equal preference
      picks the resident unit. Covered by 2.1.

## 2b. The node ranks itself on its live state

- [x] 2b.1 `_view_with_self` replaces this node's row in the fleet view with its live
      residency and per-unit queue before ranking (the broker's snapshot lagged a restart
      and priced nothing). Test: `tests/test_unit_queue.py::test_the_node_ranks_itself_on_its_live_state_not_the_snapshot`
      (fails without the overlay). Ledger: unchanged.

## 3. Rollout

- [ ] 3.1 Deploy to node-g, node-h and node-a as a layer over each node's
      current release; read a live `/harmony/route` dry run naming `saturated`.
- [ ] 3.2 Observe a saturated period spill to a second copy and the copy unload when idle.
