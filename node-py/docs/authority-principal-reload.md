# Adding and removing principals without restarting the authority

The workload authority (`python -m livestack_node.workloads.service --config F`)
re-reads the `principals` of `F` on **SIGHUP** and swaps the whole set in
atomically. Handlers, `state_dir`, `port`, `limits`, `blob_limits` and
`artifact_mirror` are NOT reloaded; changing them still needs a restart.
(A worker's handlers never need either: workers report them at `worker/report`.)

    edit authority.json
    systemctl --user reload livestack-workload-authority   # = kill -HUP $MAINPID

Outcome is in `authority.log`: `principal_reload_applied: N principals,
added=[..] removed=[..]` or `principal_reload_refused: <reason>`. Tokens are
never logged.

## Semantics

- **Validation is the startup validation** (1..128 principals, unique tokens,
  known roles, worker principals bind `worker`+`host`, callers declare
  handlers, no unknown fields), plus one reload-only rule below. Any failure
  keeps the previous set entirely and logs `principal_reload_refused`; the
  authority never loses its principals and never crashes on a bad file. A torn
  read (editor mid-write, empty or half-written JSON) is retried 3 times, 0.2 s
  apart, before it counts. Prefer write-temp-then-rename anyway.
- **Atomic swap.** A new immutable tuple replaces `server.principals` by one
  reference assignment under a lock. A request reads the set once, so
  in-flight requests keep the set they started with and none sees a mix.
- **Add.** The new worker or caller authenticates on its next request.
- **Remove.** Its next request gets 401 (unknown token), as for any unknown
  token. The authority does not kill what it owned:
  - a removed worker's running attempt is not touched; without heartbeats it
    expires through the existing lease and the job is retried or ends;
  - a removed caller's jobs stay in the database. Queued ones may still be
    placed and run; they end via their attempts/deadline limits and are pruned by
    the normal terminal retention. Nothing is orphaned forever, but nobody can
    cancel or read them until the id returns. To stop them, cancel first, then
    remove the caller.
- **Rotate** (same id, new token): the old token stops working, the new one
  works, on the next request.
- **Cap changes** (`max_running`, `on_cap`, `delegate_prefix`, handlers of an
  existing caller) apply from the next submit/placement. Running counts and all
  job/worker state live in the SQLite store, not in the principal, so
  unchanged principals lose nothing. The store's cap table is bound before the
  principal set is swapped, so a new id is never briefly uncapped.
- **Refused: changing an existing id's `role`, `worker` or `host`.** Named
  reason `principal_binding_changed`. A worker's id/host key its registered
  state and host budgets. Remove the id (reload), then add a new one (reload).
- Duplicate *ids* with distinct tokens are not rejected (neither at startup);
  the later entry wins the store's cap table. Do not do that.

## Handlers and the handler release policy

The same SIGHUP also re-reads `handlers` and `handler_release_policy` (the sections
`ReloadableConfig` names). A new handler id can therefore be installed, and given a
release policy, without a restart.

- `handlers` is **add-only**. A file that drops an installed id is refused whole
  (`principal_reload_refused: handler_removal_refused: <id> ...`): removing one could
  strand queued jobs, so that stays a restart.
- The policy is judged by the startup rules (`handler_registry.parse_policy`, one owner),
  against the handler set *after* the add, so one edit can add a handler and its policy.
- All sections are validated before any is applied. A refusal in any of them keeps the
  previous principals, handlers and policy, and logs the named reason (never a token).

## systemd

Add one line to the `[Service]` section of the unit
(`~/.config/systemd/user/livestack-workload-authority.service` in production):

    ExecReload=/bin/kill -HUP $MAINPID

then `systemctl --user daemon-reload`. The running authority only picks up the
reload code on ONE restart; do that in an idle window (a restart is what
this feature exists to avoid: it interrupts the whole fleet's leases).
Before that restart SIGHUP would kill the old process (default action), so do
not `reload` until the new code is running.

## Tests

`tests/test_workload_principal_reload.py` runs a real `WorkloadServer` (and one
real service process receiving a real SIGHUP).
