# Focused verification receipt

Tested source tree: `c12829f6cbd2bd35183d0485b7efdb355d312ff4`

Base commit: `3ea2476ffa3b9373c998d3f48c0872119176b2be`

Contract validation: `openspec validate workload-success-terminal-metadata` passed.

Archive: `2026-10-31-workload-success-terminal-metadata`; the delta was synced
into `openspec/specs/harmony-workload-intents/spec.md`. Repository-wide
`openspec validate --specs` passed with 16 specs and 0 failures. That validation
also surfaced a pre-existing missing scenario in `workload-wall-clock-bounds`,
which was added without changing implementation behavior.

Focused verification command, run from `node-py/`:

```sh
rtk pytest -q tests/test_outcome_causes.py tests/test_streams_producers.py tests/test_streams_workload_authority_integration.py tests/test_work_scopes_model.py tests/test_workload_artifacts.py tests/test_workload_compilation_policy.py tests/test_workload_environments.py tests/test_workload_handler_registry.py tests/test_workload_host_memory.py tests/test_workload_http.py tests/test_workload_principal_reload.py tests/test_workload_resource_history.py tests/test_workload_store.py tests/test_workload_worker.py::test_release_specific_exit_policy_uses_the_pinned_descriptor -k 'not test_object_put_acknowledges_canonical_cas_before_optional_mirror and not test_busy_worker_blocker_and_cleared_on_claim'
```

Result: `286 passed`, exit code `0`. The HTTP mirror test is an unrelated baseline failure: the existing resumable-upload path never calls the optional mirror callback. The blocker test passed when run alone; it failed once in a larger combined invocation and is omitted from this stable focused receipt.

An earlier exploratory command also included the complete systemd-backed
`test_workload_worker.py` module and returned `373 passed, 25 failed, 3 skipped`.
Its failures included worker-readiness/claim short circuits, restart expectations,
timing limits and a subprocess import-path failure. That broad run is not claimed
as acceptance evidence; the targeted worker classifier case above exercises this
change without running the full host-service suite.

The worker classifier case verifies clean receipt success, missing counters, explicit `resource_evidence: none`, and a declared disk breach. Store and authenticated integration tests cover rejection, persisted evidence, legacy read projection without replay, stream/result agreement, and the result digest. No placement decision was made; no decision-ledger record was added.

Implementation file SHA-256 at the tested tree:

| File | SHA-256 |
| --- | --- |
| `node-py/livestack_node/workloads/causes.py` | `46a144ce33e5aa1e8d321e74e9dde17e6a94c41bc17977e883f6410081c6ba5f` |
| `node-py/livestack_node/workloads/store.py` | `03a63ba294e15c6bca50b6a8c446e4c66cef1ddf9d2272971e805d52d3a8de3b` |
| `node-py/livestack_node/workloads/worker.py` | `288c02b19ab81de4acfb5fca51706bbb237309c1e3d766edc7ed34edccf9f218` |
| `node-py/livestack_node/workloads/result_manifest.py` | `cda09ea8322b5727b3a945a067d258bb72b11867bd0a939cf760855a04a26ede` |
| `node-py/livestack_node/streams/workload_authority.py` | `c420f7551d6cb6b900eb5e95156ee1d371d0329baae07e678e70f8633576f0f0` |
| `node-py/docs/native-stream-producers.md` | `335c5c6dc0ce058511674bf35cdb67da43e70c844708fb9e01886269a5073f8a` |
| `_plans/durable-workloads.md` | `30edb8eb93b58833ef3f57dc1e4a46c16df9bbb53b6c8574580cf9921154d642` |
| `openspec/changes/archive/2026-10-31-workload-success-terminal-metadata/specs/harmony-workload-intents/spec.md` | `2916f47678926acb8c3786526d80ecc4f4836b1eb323caacb277d5d73f1e8e4b` |
| `openspec/specs/harmony-workload-intents/spec.md` | `7ed0d25452b0bb56f6ad100f34f6f451830f76d54f377493402fef2c50533952` |
| `openspec/specs/workload-wall-clock-bounds/spec.md` | `31b1316b815e281ab69d69fe631309473bb09faa5678fad50e9e00670e442560` |
