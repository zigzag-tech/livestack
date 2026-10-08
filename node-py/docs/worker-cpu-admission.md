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


## Verified signals: `psi_some` and `runqueue` (storage-headroom-admission)

Both `loadavg` (counts uninterruptible tasks, unbounded against cores) and `psi` (`full avg60`;
CPU `full` reads 0 on kernel 7.0 here, so the gate can never fire) are poor signals.

```json
"cpu_admission": {"policy": "psi_some", "stall_some_avg10_percent": 40, "reserve_cpu": 0,
                  "selftest_seconds": 3, "fallback": "runqueue"}
```

- `psi_some`: stalled when `/proc/pressure/cpu` `some avg10` >= `stall_some_avg10_percent`
  (avg10, so a finished burst releases admission in about 10 s).
- `runqueue`: mean `procs_running` per core (minus the sampler) over `window_seconds`
  (1 Hz thread), stalled above `stall_runnable_per_core` (default 2.0). `proc_root` is a test seam.
- `fallback` (`runqueue` by default, `null` to forbid): used only when the pressure file is
  **absent** (no PSI), announced in the worker log and in the report's `cpu_signal.detail`.
  A signal that exists but does not move never falls back.
- Not stalled: `capacity.cpu - reserve_cpu`. Stalled, inert or unreadable: `0`, with the reason.

**Self-test (positive control, at worker construction):** the signal is read idle, then
`cores + 1` busy processes run for `selftest_seconds` (1-10, killed and reaped in a `finally`), and
the signal must rise by at least 1.0 (`psi_some`, percentage points) or 0.5 (`runqueue`, tasks per
core). Result in the worker report as `cpu_signal = {policy, state, detail}`:
`active`; `active_unverified` (host already above the threshold, burn skipped); `inert`
(`psi_some`/`runqueue` then offer CPU 0, naming the policy; roster warning `cpu_signal_inert`).
Legacy `psi` is also self-tested (on `full avg10`) but advisory: an `inert` result is reported
(`signal_inert`) and logged while the old gate keeps its behaviour, so an upgrade cannot withdraw a
host's CPU. Worker config is read at start; a changed `cpu_admission` takes effect on restart
(there is no worker SIGHUP reload).

Verify on a host: start the worker, read `cpu_signal` in the authority roster
(`GET /v1/workloads/workers`, per worker) -- `state: active` and a `detail` showing the rise.
