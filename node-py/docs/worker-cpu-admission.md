# Worker CPU admission (`cpu_admission`)

A worker reports `available.cpu`; placement admits a job only if its `admit.cpu`
fits in `min(capacity, available)` minus Harmony's own reservations.

Default (`loadavg`, unchanged): `available.cpu = max(0, min(capacity.cpu, cpu_count - loadavg1))`.
On a shared host with more runnable tasks than cores this is **0**, so the host
is ineligible for every job. 2026-10-06, zz-joe: load 25 on 16 cores, release
jobs queued 30-110 min with `insufficient shared host resources`. Evidence and
the earlier tower measurement: benchday `docs/e2e-admission-capacity.md`.

Opt-in `psi` policy, in `worker.json` (schema-validated, unknown keys refused at
construction, no environment variables):

```json
"cpu_admission": {"policy": "psi", "stall_full_avg60_percent": 5, "reserve_cpu": 0}
```

- Not stalled: `available.cpu = capacity.cpu - reserve_cpu`. Placement's
  reservations already account for what Harmony admitted; `reserve_cpu` is the
  floor held back for work Harmony does not place (agents' own runs).
- Stalled (`/proc/pressure/cpu` `full avg60` > `stall_full_avg60_percent`): `0`.
- File unreadable or no `full` line: `0` (never "no pressure"); logged once per
  state change (`cpu pressure ok|stalled|unreadable (psi)`).
- Linux only; Windows and macOS keep their own measurement.
- `psi_path` exists for tests.

## Rollout (per worker, at idle; not done automatically)

1. Confirm the host: `cat /proc/pressure/cpu` shows a `full` line (kernel >= 5.13).
2. Pull this livestack revision on the host's checkout.
3. Add the `cpu_admission` block to each worker's `worker.json`
   (zz-joe: `worker.json`, `worker-2.json`, and the release worker's file).
4. Restart at idle: `systemctl --user restart livestack-workload-worker.service`
   (and `-2`, the release unit), only when no attempt is running.
5. Verify: the worker log shows `cpu pressure ok (psi)`, and the authority's
   worker report has `available.cpu` equal to capacity under load.
6. Roll back by deleting the block and restarting.

Caveat: admission now relies on Harmony's reservations, so non-Harmony load
(agent emulators, test runs) is no longer a refusal; cap it separately (a systemd
slice for agent runs) or raise `reserve_cpu`.

Prerequisites: none beyond stock `python3`. The `cpu_admission` block is judged by plain
Python (`livestack_node/workloads/cpu_admission.py`), so the worker needs no third-party
package for it (pydantic is the authority's dependency, not the worker's). An earlier
revision (70a11344) imported pydantic and crash-looped hosts without it; releases built from
a later commit do not. `tests/test_workload_worker.py::test_worker_startup_path_needs_no_pydantic`
holds that line: do not import a third-party package on the worker startup path.
