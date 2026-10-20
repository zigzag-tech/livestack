# Local compatibility and worker-lifecycle refresh

Run on `lappy-bellinzona` at 2026-10-11T03:03Z.

The existing deterministic compatibility controls passed:

```text
node-py/.venv/bin/python -m pytest -q node-py/tests/test_task_environments.py \
  -k 'profile_probe_refresh_invalidates_cache_after_toolchain_replacement or machine_abi_change_invalidates_cache_before_reuse'
2 passed, 30 deselected
```

These fixtures verify toolchain-probe changes and an ABI identity change
invalidate the retained cache before reuse. The ABI case patches
`platform.machine()`; it is not a real cross-host ABI acceptance.

Two existing private worker lifecycle controls were first attempted with the
default pytest temporary directory under `/tmp`; that path is hidden from the
attempt unit by its `PrivateTmp` mount, so those failures did not establish a
systemd-manager defect. A direct bounded `systemd-run --user` positive control
then succeeded, and no test attempt unit remained. Re-running both tests with a
home-directory pytest base passed:

```text
PYTHONPATH=node-py node-py/.venv/bin/pytest -q \
  --basetemp=/home/ubuntu/.cache/benchday-taskenv-acceptance-20261011-0438 \
  node-py/tests/test_workload_worker.py::test_scope_close_cancels_task_environment_descendants_after_cleanup \
  node-py/tests/test_workload_worker.py::test_worker_and_authority_restart_rebuild_unconfirmed_task_environment
2 passed in 6.92s
```

These remain local fixture controls, not proof from the installed handler.
Real admitted compiler/toolchain and cross-ABI invalidation, installed-handler
stale-receipt rejection, canceled-descendant cleanup, and worker rollout remain
open. The release-3448 correction and latest live canary/worker readback are
recorded in `release-status-correction-20261011.md` and
`live-runtime-readback-20261011-0559utc.md`.

## Continuation reruns (2026-10-11)

The stale-receipt/restart and canceled-descendant controls were rerun serially
with a home-directory pytest base and passed 2/2 in 10.97 s. These are local
fixture tests, not installed-worker acceptance.

The full `tests/test_workload_worker.py` suite then ran alone with a
home-directory base: 73 passed, 6 skipped, 4 failed in 235.53 s. The failures
were the output-bound test's stale `transfer.transport.dial` patch path, the
oversized-result test receiving `WorkloadError`, a retry-log assertion that did
not match the emitted log, and a disk-delta positive control measuring zero.
The targeted two controls passed in this run. The full suite is not green; these
four failures remain to be triaged before using it as completion evidence.

Source inspection confirmed the oversized-result failure's cause: `transfer.py`
raises generic `WorkloadError(..., 413)`, while `worker.py` converts only the
typed `ArtifactTooLarge` exception into the terminal infrastructure result.
The output-bound test also patches the old `transfer.transport.dial` location;
the current transport is imported by `route_kinds.py`. The retry case did
successfully retry and complete, but its assertion expects an obsolete log
message. The disk-delta zero remains unexplained and needs a valid filesystem
positive control before that assertion can be trusted.

The state/authority fixture files were also rerun serially with a
home-directory base:

```text
node-py/.venv/bin/pytest -q tests/test_task_environments.py \
  tests/test_workload_environments.py
58 passed in 52.33s
```

This is local state-machine/authority fixture evidence. It does not prove
admitted compiler execution, cross-host ABI invalidation, or installed-worker
rollout.
