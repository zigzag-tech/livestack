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

- [x] 3.1 Deployed 2026-11-25 to node-g, node-h and node-a as a layer
      (`<running layer>-saturated-selfview`, drop-in `zzzzzzz-saturated.conf`; delete it to
      roll back). Live dry run on node-g with 2 in flight and 34.8 s of queue:
      `forward saturated`, every target priced by `finish_s`.
- [x] 3.2 Observed: 4 long requests on node-g spilled to the tower's second card, which
      loaded Inkling in ~20 s, served (Jev 46.6 s cold, then 2.4 s and 0.1 s), unloaded at
      06:52 when idle and loaded again on the next overflow at 06:56. Caveat seen: for the
      first minutes after a node restarts, the broker's view has no load/decode figures for
      it, so it is unpriced and skipped until it reports them.
