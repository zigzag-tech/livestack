## 0. Prerequisite

- [x] 0.1 Settle harmony-llm's source of truth: it is already `node-py/examples/harmony-llm/server.py`
  (symlinked from `~/harmony-llm`); only xc-tower-ubuntu runs it (design §9).
  Tests: none. Ledger: none. Verify: `readlink ~/harmony-llm/server.py`.

## 1. Measured-cost fixtures (pure, no deployment)

- [x] 1.1 `vllm_startup.py`: parse the five startup memory lines into `MeasuredCost`, or
  `unknown` with the unmatched line names.
  Tests: fixtures from the 2026-09-28 journal (bf16/1-adapter and fp8/2-adapter,
  vLLM 0.28.0); a truncated log gives `unknown` naming the missing lines.
  Ledger: none. Verify: `pytest node-py/tests/test_vllm_startup.py`.
- [x] 1.2 `composition_hash()`: canonical JSON over base, sorted adapters with ranks, kv
  dtype, max context, batch cap and engine version.
  Tests: stable under adapter order; changes with any field.
  Ledger: none. Verify: `pytest node-py/tests/test_composition_hash.py`.

## 2. Measured unit cost (harmony-llm)

- [x] 2.1 Capture the engine's stdout up to ready, parse it with 1.1, and report on
  `/residence` with `source`, `composition_hash` and `engine_version`; persist the last
  row per hash to `unit-costs.jsonl` (256-row bound).
  Tests: fake engine emitting fixture lines, then `/residence` shows the parsed values;
  unparseable output shows `measured: "unknown"` and increments the counter.
  Ledger: none (the node reports; the broker records). Verify: `curl /residence | jq .units[].measured`.
- [x] 2.2 (reopened, then fixed: design §8c) `RestPeer.units` uses the measured footprint when present, the declared value
  as `source: "declared"` otherwise, and the device budget when `unknown`.
  Tests: planner unit test for all three sources, including that `unknown` blocks
  co-placement on that device.
  Ledger: the `plan()` candidate rows carry footprint `source`. Verify: `pytest node-py/tests/test_planner_measured.py`.
- [x] 2.3 **[ASK]** Deploy to xc-tower-ubuntu. Confirm `llm_general` reports the
  measured cost and that `plan()` output is unchanged except for the footprint value.
  Tests: 2.1–2.2 green. Ledger: the first plan after deploy shows `source: vllm-startup`.
  Verify: `GET /plan` before/after diff.

## 3. Demand log (harmony-llm)

- [x] 3.1 Writer: bounded queue, one drain task, rotation (64 MiB × 8), a 21-day age
  window that fails closed when unset, and `demand_log_dropped` on `/residence`.
  Tests: unset window leaves logging disabled and still serves; a full queue drops and
  counts; a record older than the window is deleted on rotation.
  Ledger: none. Verify: `pytest node-py/tests/test_demand_log.py`.
- [x] 3.2 Proxy path: one record per forwarded request (chat, completions, classifier),
  with `usage` → tokens, `null` when absent, and `owner_ns` only.
  Tests: records for base, adapter and classifier requests; a streamed request without
  usage gets `null`; no owner id appears anywhere in the file.
  Ledger: none. Verify: replay the fixture requests, then check with `jq`.
- [x] 3.3 **[ASK]** Deploy with logging on (DEPLOYED 2026-09-28 04:58 UTC; count check
  2026-09-30, see receipts/demand-log-count-check.md: every proxied request recorded,
  0 dropped; the counter gap is `n`, now recorded). After 24 h, check the record count against
  vLLM's `vllm:request_success_total` delta for the same window.
  Tests: 3.1–3.2. Ledger: none. Verify: counts agree within the dropped counter.
- [x] 3.4 Add both logs to the storage inventory in `HARMONY.md` (bound and enforcer).
  Tests: none. Ledger: none. Verify: doc review.

## 4. Replayable `plan()` records

- [x] 4.1 Canonical `WorldState` serialisation and a snapshot store (sha256, gzip,
  dedupe, age window + 512 MiB cap).
  Tests: round-trip equality; identical states share one file; the cap evicts oldest.
  Ledger: none. Verify: `pytest node-py/tests/test_snapshots.py`.
- [x] 4.2 `_emit_plan` references the snapshot on every action record.
  Tests: a `plan_and_apply` integration test finds `snapshot` on each row.
  Ledger: **adds** `snapshot` to plan records. Verify: `jq .snapshot` on the ledger after one tick.
- [x] 4.3 `replay_plan.py`: reload a snapshot, re-run `plan()`, and diff against the
  recorded actions. Report mismatches.
  Tests: 1,000 recorded ticks from a test broker give 0 mismatches.
  Ledger: reads only. Verify: `python -m livestack_node.replay_plan --since 1h`.

## 5. The composition problem (pure)

- [x] 5.1 `composition.py`: `CompositionState`, `Composition`, `feasible` (the five
  hard rules plus `filtered:hard_pin`), and memory prediction (exact row, else additive
  deltas marked `estimated`, else `Unknown`).
  Tests: the design's calibration fixture (chips bf16 exact; chips + jemm bf16
  infeasible `kv_tokens<max_model_len`; chips + jemm fp8 feasible); a never-measured base
  gives `Unknown`; no weight changes any feasibility result (property test).
  Ledger: none. Verify: `pytest node-py/tests/test_composition.py`.
- [x] 5.2 `composition_replay.py`: the KV-pool, batch-cap and adapter-slot admission
  model.
  Tests: the structural claims on a trace shaped like the journal fixture (max running
  saturates near 11 at 29,749 KV tokens); a synthetic trace with a known queue gives the
  exact expected delay; the journal's waiting-below-full-KV is pinned as a strict xfail
  (design §6); true replay-vs-journal validation is a skipped test naming the missing
  demand log.
  Ledger: none. Verify: `pytest node-py/tests/test_composition_replay.py`.
- [x] 5.3 `cost()` and the weights artifact v1 (hand-set, reviewed), scored over several
  past windows (mean and worst).
  Tests: each term moves in the right direction on constructed traces; the change cost
  dominates a marginal gain, so `keep` is chosen.
  Ledger: none. Verify: `pytest node-py/tests/test_composition_cost.py`.
- [x] 5.4 `Composer` protocol, `ExhaustiveComposer`, and `run_composition()` (the live
  composition always scored; choose only when the gain exceeds the change cost).
  Tests: a proposer returning an infeasible candidate has it filtered, never chosen;
  a deterministic replay reproduces the decision.
  Ledger: none (pure). Verify: `pytest node-py/tests/test_composer.py`.

## 6. Decisions, outcomes, and the dry-run endpoint

- [x] 6.1 `python -m livestack_node.compose`: fetch each node's `GET /composition/facts`,
  build the state, run the composer, write ONE ledger decision (own file, design §8a),
  store the facts as its snapshot, print the proposal and a units-file diff. Dry run;
  applies nothing. `--replay <id>` re-runs a recorded decision from its snapshot.
  Tests: `tests/test_compose.py` (the 2026-09-28 scenario proposes chips + jemm/fp8 with
  the diff; bf16 two-adapter recorded infeasible; replay reproduces; no jemm demand
  keeps); `tests/test_harmony_llm_measured_demand.py` (facts route).
  Ledger: **new** `emitter: composition, decision: compose`. Verify: `python -m livestack_node.compose --no-record`.
- [x] 6.2 Outcome joiner: `measured` (predicted-vs-measured per term), hourly `served`
  for 24 h, and `not_applied` after 7 days.
  Tests: a measurement for the chosen hash writes one `measured` row with the
  `parent_decision_id`; none for 7 days writes `not_applied`.
  Ledger: **new** outcome rows. Verify: `jq 'select(.parent_decision_id)'` on the ledger.
- [x] 6.3 **[ASK]** First real run on xc-tower-ubuntu. Record the proposal as a receipt,
  including whether it would have kept today's chips + jemm/fp8 composition.
  Tests: 6.1–6.2. Ledger: one `compose` decision. Verify: the receipt in `receipts/first-run.md`.

- [x] 6.4 Hourly `compose --outcomes` via `livestack-compose-outcomes.timer`
  (systemd oneshot + timer, `Persistent=true`), installed on xc-tower-ubuntu.
  Tests: a manual run in a clean env writes `{"outcome_rows_written": 0}` (no
  proposal has chosen a change yet). Ledger: outcome rows under
  `parent_decision_id`. Verify: `systemctl list-timers livestack-compose-outcomes.timer`.

## 7. Docs and archive

- [x] 7.1 `HARMONY.md`: measured cost and its sources, the demand log, reading a
  composition proposal, and applying one by hand against the gates in spec
  `unit-composition`. Mark `_plans/resource-planner.md` §2's stale sentence.
  Tests: none. Ledger: none. Verify: doc review.
- [x] 7.2 `openspec validate harmony-placement-foundation --strict`, then archive (2026-09-30).
  Tests: all of the above green. Ledger: none. Verify: the command exits 0.
