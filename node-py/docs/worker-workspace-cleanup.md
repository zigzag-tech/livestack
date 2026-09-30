# Worker workspace cleanup never blocks claiming

Incident 2026-09-29: `xc-mac-studio-harmony` sat wedged for hours logging only
`worker waiting after PermissionError`. `WorkloadWorker.reconcile()` called
`shutil.rmtree` on a leftover attempt workspace holding a read-only file in a
read-only directory (e2e handlers unpack read-only source trees). The exception
escaped `reconcile()` and `step()`, the same workspace was retried first every
turn, and claiming/heartbeats never ran.

Now (`workloads/worker.py`):
- `rmtree_writable` chmods directories `u+rwx` and retries once.
- `_remove_workspace` catches any removal error, keeps the workspace in
  `self.stuck_workspaces`, logs ONE line per distinct (path, error):
  `workspace cleanup failed: <path>: <Type: error> (N un-removable workspaces, B bytes)`,
  and `step()` retries stuck workspaces each turn. A cleanup ack is still sent for
  an attempt whose workspace is stuck (the attempt is stopped), so the authority
  keeps offering work.
- `worker_service.serve` logs the full traceback once per (exception type,
  raise location) as `worker wait cause (first occurrence)`.

The authority's report schema is closed, so the stuck count/bytes are in the log
line only, not the register payload.

## Docker runtime dir left by an OOM-killed attempt (2026-09-30, `xc-win-1-wsl`)

An e2e attempt was OOM-killed; afterwards the worker looped on
`worker waiting after WorkloadError: unrecognized Docker runtime; cleanup refused`
and held capacity. `docker_runtime.cleanup(unit)` refused the leftover
`/run/user/<uid>/hw-<hash>` (only rootlesskit sockets, no `owner.json`), and the
refusal escaped `executor.stop()` into `reconcile()`/`step()` forever.

Now (`workloads/docker_runtime.py`, `workloads/worker.py`):
- The directory name is `hw-sha256(unit)[:24]` under the worker's own
  `/run/user/<uid>`, so a name match is the ownership proof. `cleanup` removes it
  when it is a real directory (not a symlink), owned by the worker's uid, has no
  readable `owner.json` naming a DIFFERENT unit (missing/garbled marker is fine),
  and no process of ours uses it (scan of `/proc/*/{cwd,root,exe,fd/*}` plus bound
  paths in `/proc/net/unix`). One line: `docker runtime cleanup: removed <path>
  (no owner.json: attempt died before writing it)`.
- Still refused, named (`RuntimeCleanupRefused`, a `WorkloadError`): symlink or
  foreign uid, `Docker runtime owner mismatch`, `in use by pid N`, removal errors.
- `cleanup` runs only after the unit is gone and its cgroup unpopulated, so every
  `RuntimeCleanupRefused` means capacity is safe to release. The worker's
  `_stop(attempt)` therefore catches it, keeps the attempt in `stuck_runtimes`,
  logs ONE `docker runtime cleanup failed for attempt ...` line per distinct
  cause, retries each step, and never blocks claiming. Other `stop()` errors
  (unit or cgroup still alive) still block, as they must.
- `prepare` writes `owner.json` atomically (temp + `os.replace`) immediately
  after `mkdir`, before rootlesskit starts; it still refuses a pre-existing
  directory (`exist_ok=False`). Unit names are per attempt; a leftover of the
  same unit is removed by `stop()` first.
- Tests use `LIVESTACK_WORKLOAD_RUNTIME_BASE` in place of `/run/user/<uid>`;
  production never sets it. Tests: `tests/test_docker_runtime_cleanup.py`,
  `test_refused_runtime_cleanup_never_blocks_claiming_and_logs_once`.
- Not covered: processes of other uids are unreadable in `/proc` and are skipped.
