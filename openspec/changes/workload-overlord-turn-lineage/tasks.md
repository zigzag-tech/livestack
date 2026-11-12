Checks: `cd node-py && uv run pytest -q tests/test_workload_turn_lineage.py tests/test_workload_job_changes.py tests/test_workload_store.py tests/test_streams_workload_authority_integration.py`

## 1. Preserve the authenticated turn source

- [x] 1.1 Add the optional closed `source: {kind: "overlord_turn", turn_id}` submit field, bounded id validation, and default-off `source_turns` principal capability enforced on stream and direct HTTP submissions; verify authorized, unauthorized, malformed, extra-key and legacy cases in `test_workload_turn_lineage.py`. Ledger: the existing intent ledger records the validated source before dispatch; no placement decision changes.
- [x] 1.2 Forward the validated source into WorkloadStore's canonical idempotency input; verify replay with the same source is idempotent and replay with a different source is refused without changing the original in `test_workload_turn_lineage.py` and `test_streams_workload_authority_integration.py`. Ledger: use the existing intent ledger; add no second decision record.

## 2. Persist job provenance and exact attempt identity

- [x] 2.1 Add nullable source columns and an exact-source index with additive startup migration; verify a legacy database opens with null lineage and a submitted source survives close/reopen in `test_workload_turn_lineage.py`. Ledger: provenance is durable job state, not a placement decision.
- [x] 2.2 Ensure a worker cannot set or replace source provenance and preserve the authenticated boot id on each claimed attempt; verify a retry exposes the boot id stored for its exact attempt in `test_workload_turn_lineage.py`. Ledger: existing workload admission and attempt decisions remain unchanged.

## 3. Expose a bounded owner-scoped lineage read

- [x] 3.1 Add exact `source_turn_id` filtering to the fixed-owner job reader before row limiting; verify another source and another owner are never returned, query work uses the source index, and a complete empty page is distinguishable from truncation in `test_workload_turn_lineage.py` and `test_workload_job_changes.py`. Ledger: the reader is read-only and emits no decision entry.
- [x] 3.2 Include source id, exact job/attempt/worker/boot identity, and a closed state explanation with its observation time; verify handler error prose, progress text, logs and artifact data are absent while queued/running/terminal states remain named in `test_workload_turn_lineage.py`. Ledger: these fields project authority-owned state and do not create a new decision.

## 4. Validate lineage in WorkloadClient

- [x] 4.1 Add a dedicated exact-turn reader that validates the expected owner, exact source id, bounded response, row identities and safe explanation schema; verify wrong-owner, mismatched-source and malformed responses fail closed in `test_workload_turn_lineage.py`. Ledger: client reads only and adds no decision entry.
- [x] 4.2 Preserve pagination and incomplete-result semantics in the client; verify truncated pages cannot be returned as a complete lineage result and exact empty results remain complete in `test_workload_turn_lineage.py`. Ledger: client reads only and adds no decision entry.

## 5. Document and verify the cross-boundary contract

- [x] 5.1 Update `HARMONY.md`, `node-py/docs/workload-job-source.md` and `_plans/durable-workloads.md` with the source owner, exact-turn reader, bounds, unknown semantics and privacy boundary; verify each names the optional source field and prohibits inferred joins. Ledger: documentation only; no entry.
- [x] 5.2 Run the `Checks:` command plus `openspec validate workload-overlord-turn-lineage`; verify stream, restart, owner isolation, attempt retry and truncation controls all pass. Ledger: tests observe existing intent and workload state; no new decision record is expected.
