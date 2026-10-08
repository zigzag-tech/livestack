## Why

On 2026-10-07/08 failed Benchday release trains left Harmony jobs behind. Daemon build jobs for builds 3391, 3392
and 3393 stayed `queued`/`running` at the authority after the release plans that submitted them had failed. They
occupied both release workers and starved the live train, which showed no progress for 28 minutes. Later, jobs an
operator had cancelled "reappeared".

The authority cannot prevent any of this because a job has an `owner` (the submitting principal) and nothing else:

1. **A job has no owner above the principal.** `jobs.owner` is the dispatcher account, shared by every run, plan and
   train that account ever submits. When the thing that wanted the work ends (a plan fails, a run is abandoned, the
   coordinator dies), no authority-side fact says which jobs that was. The only tool is `cancel`/`withdraw` one job id
   at a time, and only if the caller remembered the id. `store.py` `cancel` and `withdraw` take a single job.
2. **A cancelled job is invisible to idempotent resubmission.** `submit` returns the existing row for `(owner, key)`
   whatever its state. A caller that maps `cancelled` to "infrastructure, retry" (ZZOPS's `harmony-executor.mjs` did)
   and keeps the same key re-reads the cancelled job; one that derives a new key per attempt creates fresh work for a run
   that was cancelled. Either way, cancellation did not stick.
3. **Nothing expires work whose owner vanished.** `_expire` fails jobs on an absolute `deadline` and attempts on a
   heartbeat lease. A queued job with no deadline whose submitter died waits forever; a running job whose submitter died
   keeps its worker for the handler's `max_seconds` (up to an hour).

**Design records realised:** `_plans/durable-workloads.md` (job lifecycle, cancel/withdraw, expiry). **Stale in it:** it
describes cancel as per job and owner as the principal only; after this change a job may also belong to a scope that
can be closed, and expiry has a third source (a scope lease).

## What Changes

1. **Work scopes.** A submission (schema `version: 4`) may name `scope: {key, lease_seconds?}`. A scope is `(owner principal, key)`, opened
   by the first submission naming it, with state `open` or `closed`. The key is opaque to Livestack (a caller encodes
   its own run, plan or train id); nothing in Livestack knows what a Benchday or ZZOPS run is.
2. **Close cascades.** `POST /v1/scopes/<key>/close {reason}` closes the scope and, in the same transaction, cancels
   every non-terminal job in it with the existing `cancel` semantics (running attempts move to `cleanup`, the worker
   is held until it reports clean). It is idempotent and returns counts.
3. **A closed scope accepts no work.** `submit` naming a closed scope is refused `409 scope_closed`, including a replay
   of an existing key in that scope. Cancellation therefore sticks against retry loops and resubmission.
4. **Scope leases.** `lease_seconds` (bounded by `limits.scope_lease_max_seconds`) makes the scope expire unless the
   owner renews it with `POST /v1/scopes/<key>/renew`. `_expire` closes an expired scope with reason `lease expired`
   through the same cascade. No lease means no expiry (a scope can still be closed explicitly).
5. **Visible state.** `GET /v1/scopes/<key>` reports state, lease, close reason and per-state job counts. Each job view
   carries `scope`. A job cancelled by cascade carries cause `scope_closed` (see change `typed-outcome-causes-and-blockers`).
6. **Opt-in requirement.** `limits.scope_required_handlers` (same shape as `describe_required_handlers`) makes a
   handler refuse unscoped submissions, switched on only after every submitter sends a scope.

## Capabilities

New capability `work-scopes`.

## Impact

`workloads/model.py` (submission validation, `Limits`), `workloads/schema.sql` (+`scopes` table, `jobs.scope`, additive
migration like the existing `ALTER TABLE` guards in `store.py`), `workloads/store.py` (`submit`, `_expire`, new
`close_scope`/`renew_scope`/`get_scope`, `_prune`), `workloads/http.py` (three routes), `workloads/client.py`. Additive
on the wire: a submitter that sends no scope behaves as today. Companion: ZZOPS change `run-settlement-and-ownership`.
Not changed: placement, worker, handler contract.
