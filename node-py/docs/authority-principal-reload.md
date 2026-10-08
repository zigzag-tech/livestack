# Adding and removing principals without restarting the authority

The workload authority (`python -m livestack_node.workloads.service --config F`)
re-reads `principals`, installed `handlers`, `handler_release_policy`, and an
optional `environment_handlers` policy map on **SIGHUP**. This applies policy
changes without interrupting active leases. `state_dir`, `port`, `limits`,
`blob_limits` and `artifact_mirror` are NOT reloaded; changing them still needs
a restart. Worker handler lists do not need an authority restart: workers report
them at `worker/report`.

    edit authority.json
    systemctl --user reload livestack-workload-authority   # = kill -HUP $MAINPID
    python -m livestack_node.workloads.cli --config C reload-status   # did it take? (GET reload/status)

**SIGHUP is the only trigger.** The authority never watches the file; an edit that is not followed by
SIGHUP is not read. `GET reload/status` (admin or rollout principal) answers "applied or silently not
read": `applied` (last applied time and file hash), `last_attempt` (including a refusal's reason), the
hash of the file now, and a `verdict` of `applied`, `edited_not_applied`, `refused_current_file` or
`unknown`.

**What is NOT reloaded from the file at all:** worker claims (drain/enable) and rollout state live in the
authority database and are changed through the API (`claims/...`, `rollout/...`), see
`worker-claims-and-rollout.md`. `claim_enabled` in the file is still honoured during the migration: it is
imported once for a worker with no claims row, and a later *change* of that value is applied as a claim
change by owner `file:authority.json` with no expiry (logged `claim_file_edit_applied:<worker>`); an
unchanged file value never overrides the database (logged `claim_enabled_in_file_ignored:<worker>`).

Outcome is in `authority.log`: `principal_reload_applied: N principals,
added=[..] removed=[..]` or `principal_reload_refused: <reason>`. The applied
line also reports whether environment policy was unchanged or how many entries
were installed. Tokens and policy contents are never logged.

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
- **Environment policy changes.** `environment_handlers` is validated against
  the installed handler set and the same task-policy rules used at startup.
  SIGHUP validates the replacement against the handler set after any add in the
  same reload, then swaps the complete policy map by one reference assignment.
  An omitted section preserves the current map, so an older editor can rotate
  principals safely. An explicit empty object disables all environment
  enrollment; removing only eligible development/task-E2E entries stops new
  environment submissions while accepted jobs finish under their assignment.
  Clients see the updated eligible/forbidden capability lists on their next
  fresh negotiation. Keep full-E2E and release entries marked forbidden when
  retaining their named `environment_scope_forbidden` refusal.
- **Refused: changing an existing id's `role`, `worker` or `host`.** Named
  reason `principal_binding_changed`. A worker's id/host key its registered
  state and host budgets. Remove the id (reload), then add a new one (reload).
- Duplicate *ids* with distinct tokens are not rejected (neither at startup);
  the later entry wins the store's cap table. Do not do that.

## Handlers and the handler release policy

The same SIGHUP also re-reads `handlers`, `handler_release_policy` and
`environment_handlers` (the sections `ReloadableConfig` names). A new handler id
can therefore be installed, given a release policy and environment purpose,
without a restart.

- `handlers` is **add-only**. A file that drops an installed id is refused whole
  (`principal_reload_refused: handler_removal_refused: <id> ...`): removing one could
  strand queued jobs, so that stays a restart.
- The release policy is judged by the startup rules
  (`handler_registry.parse_policy`, one owner), against the handler set *after*
  the add, so one edit can add a handler and its release policy.
- The environment policy is judged by `WorkloadStore`'s startup validation
  against that same post-add handler set. It can be replaced or cleared without
  removing installed handler ids; a refusal keeps the old policy.
- All sections are validated before any is applied. A refusal in any of them keeps the
  previous principals, handlers and policies, and logs the named reason (never a token).

## systemd

Add one line to the `[Service]` section of the unit
(`~/.config/systemd/user/livestack-workload-authority.service` in production):

    ExecReload=/bin/kill -HUP $MAINPID

then `systemctl --user daemon-reload`. The running authority only picks up new
reload code after its normal release upgrade. Make that one upgrade in an idle
window with no running or cleanup attempts; a restart interrupts leases. After
the new code is running, use SIGHUP for principal, handler, release-policy and
environment-policy changes.

## Tests

`tests/test_workload_principal_reload.py` runs a real `WorkloadServer` (and one
real service process receiving a real SIGHUP).
