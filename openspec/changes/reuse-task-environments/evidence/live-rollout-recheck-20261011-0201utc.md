# Live worker rollout and agent identity recheck

Read-only snapshot at 2026-10-11T02:01:38Z.

- The authority roster reports 27 registered workers and 22 ready.
- All five Rust-capable workers are Linux workers on host zz-joe. No
  Rust-capable worker on another ABI is registered.
- Only zz-joe-e2e-1 and zz-joe-e2e-2 advertise task-specific E2E.
- Controller spec generation is 2, report generation is 6096, and mode remains
  observe. Applied actions are empty. The target remains unit-f38a7baa;
  zz-joe-e2e-1 is behind and workers 2–5 are unknown.
- benchday agent whoami returns unresolved=no_agent_pane. The session has no
  admitted Benchday agent identity for writes. No controller or worker write
  was attempted.

The authority activation and one-check canary remain successful. This recheck
does not change that result, but confirms that worker rollout and cross-ABI
acceptance are still pending. Automatic selection remains disabled.
