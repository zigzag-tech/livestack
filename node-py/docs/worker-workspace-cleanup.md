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
