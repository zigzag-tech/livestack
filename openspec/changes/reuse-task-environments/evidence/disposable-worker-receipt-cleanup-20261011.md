# Disposable worker receipt and cleanup controls — 2026-10-11

Focused current-worktree checks for the local receipt and process-cleanup path.
These exercise a disposable HTTP/SQLite authority and worker fixture; they are
not reads from an installed shared worker.

## Commands and result

The user systemd manager was degraded, but a transient user scope positive
control succeeded:

```sh
systemd-run --user --scope --quiet /usr/bin/true
```

The focused tests then passed 3/3 in 16.81 seconds:

```sh
node-py/.venv/bin/python -m pytest -q \
  --basetemp=/home/ubuntu/.cache/taskenv-livestack-20261011 \
  node-py/tests/test_workload_worker.py::test_worker_and_authority_restart_rebuild_unconfirmed_task_environment \
  node-py/tests/test_workload_worker.py::test_cancel_running_job_reconciles_before_readvertising_capacity \
  node-py/tests/test_workload_worker.py::test_scope_close_cancels_task_environment_descendants_after_cleanup
```

## Covered outcomes

- Restart recovery rebuilds an unconfirmed environment, keeps partial cache
  bytes out of the next result, and refuses a late stale-generation completion
  with HTTP 409.
- Cancellation waits for the child cgroup to empty before capacity is
  readvertised; the environment becomes `rebuild_required` and no cleanup claim
  remains.
- Scope-close cancellation reaches task descendants and completes cleanup
  before the test reports terminal cancellation.

These checks strengthen disposable-worker evidence only. Installed-handler
stale-receipt rejection, admitted canceled-descendant acceptance, and live
worker/quota readback remain open under the paired rollout tasks. No shared
worker, authority, unit, quota, policy, or automatic-selection setting was
changed.
