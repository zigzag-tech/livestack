## 1. Which process owns what

The **authority** (WorkloadStore, SQLite) owns scope state, the lease clock and the cascade. The **submitter** owns
the meaning of the key and is the only party that renews. Workers never see scopes: a cascade-cancelled attempt reaches
a worker exactly as an owner `cancel` does today (attempt `cleanup`, worker `ready=0` until it reports clean). Ids cross
the boundary as `scope.key` in the submission and `jobs.scope` in the row.

## 2. Data

    scopes(owner TEXT, key TEXT, state TEXT CHECK(state IN ('open','closed')),
           lease_seconds REAL NULL, lease_expires REAL NULL,
           close_reason TEXT NULL, closed_by TEXT NULL, created REAL, updated REAL,
           PRIMARY KEY(owner, key))
    jobs.scope TEXT NULL            -- key; (owner, scope) names the row; index jobs_scope(owner, scope, state)

`jobs.scope` is part of the idempotency hash: the same key resubmitted into a different scope is a conflicting request
(409), as with any other differing input.

## 3. Submit

Inside the existing IMMEDIATE transaction: (a) the `(owner, key)` replay check runs first and, if the existing job's
scope is closed, answers `409 scope_closed` instead of returning the job; (b) a new job with `scope` upserts the scope
row open (renewing the lease when `lease_seconds` is given); (c) a closed scope refuses.

Why refuse the replay of an existing key rather than return it: the replay of a cancelled job is what let a caller's
retry loop read the corpse and treat it as an infrastructure failure. Refusal gives the caller a distinct, typed
answer: "the owner you work for is gone, stop", never "the infrastructure failed, try again".

## 4. Close and cascade

`close_scope(owner, key, reason, by)`: one transaction. Idempotent: closing a closed scope returns the stored result
with `replayed: true`. For every job with that `(owner, scope)` in `queued` or `running`, apply the `cancel` body that
exists today (shared helper, no second implementation), recording reason `scope <key> closed: <reason>` and cause
`scope_closed`. Terminal jobs are never touched. Result: `{state, cancelled, running_cleanup, already_terminal}`.
A close of an unknown scope creates it closed (so a later submission by a slow submitter is refused: close-before-submit
is not a race the cancel can lose).

Bound: a scope holds at most `limits.scope_jobs` jobs (default 256, hard ceiling 1024); submission beyond refuses
`429 scope_capacity`. The cascade is therefore O(256) per call, in one transaction, no per-job round trip.

## 5. Lease and janitor

`_expire` already runs in every submit, claim, heartbeat, get and sweep transaction. It gains one statement:
`SELECT owner,key FROM scopes WHERE state='open' AND lease_expires<=? LIMIT 16`, closed through the §4 helper with
reason `lease expired` and `by='authority'`. The LIMIT bounds work per call; the remainder is closed by the next call
(idle authorities are swept by the existing periodic `sweep`). The janitor is therefore not a new loop and has no
new failure mode: the cascade is the same code as an explicit close.

`renew_scope` extends `lease_expires = now + lease_seconds` for an open scope; renewing a closed scope answers
`409 scope_closed` (the owner learns it lost the scope on its next renewal, not by silence). Renewal is cheap and
idempotent; the clock is the authority's own, so submitter and authority clocks need not agree.

`lease_seconds` bounds: `1 <= lease_seconds <= limits.scope_lease_max_seconds` (default 6 h). A scope with no lease never
expires.

## 6. Bounds and enforcers (rule: all storage bounded)

| What | Bound | Enforcer |
|---|---|---|
| Open scopes per owner | `limits.scopes_per_owner` (default 256, ceiling 1024) | `submit` refuses `429 scope_capacity` |
| Jobs per scope | `limits.scope_jobs` (256, ceiling 1024) | `submit` |
| Closed scope rows | kept while any job references them, then `terminal_seconds` (14 d) after `updated` | `_prune`; `terminal_seconds=None` deletes nothing (existing fail-closed convention) |
| Scope key / reason | 160 chars (existing `name()` pattern) / 512 chars | `model.name`, truncation with `...` marker |

## 7. Failure and observability (rule 13)

- Unknown scope on `GET`: `404 scope_not_found`. A scope that exists with zero jobs reports zeros. Absence and
  emptiness differ.
- A close that cancels N jobs logs one line `scope_closed owner=<> key=<> by=<> reason=<> cancelled=<> cleanup=<> terminal=<>`
  and writes one decision-ledger record (`decision='scope_close'`, per `_plans/decision-ledger.md`) so "who killed my job"
  is answerable after the scope rows are pruned.
- Lease expiry logs the same line with `by=authority`.

## 8. Invariants (no TLA+ in this repo; model-based property test instead)

`tests/test_work_scopes_model.py` drives a `WorkloadStore` with a fake clock through random sequences (seeded, 2,000
sequences x 40 steps) of submit, claim, complete, heartbeat, close, renew, advance-clock, restart-authority and asserts after
every step:

- S1: no job with a closed scope is `queued` or `running`.
- S2: a submission naming a closed scope never creates or returns a job.
- S3: close is idempotent (second call changes no row).
- S4: a terminal job's row is byte-identical before and after any close.
- S5: an open scope with a lease whose `lease_expires` has passed is closed by the next transaction.
- S6: counts in `GET scope` equal a recount from `jobs`.

Positive control (Benchday jidoka): the same test run against a store with the cascade statement removed fails S1.

## 9. Rollout

Additive schema (guarded `ALTER`/`CREATE IF NOT EXISTS`). A submission carrying `scope` uses submission `version: 4`
(`model.submission` accepts `{1,2,3}` today and refuses unknown fields, so an old authority refuses a scoped submission
loudly rather than silently ignoring the scope). `capabilities` gains `scopes: {version: 1, max_lease_seconds, ...}`; a
submitter probes it and, when absent, says so in its own status and falls back to explicit per-job cancel. Authority
first; submitters adopt later; no flag day. Version 4 is otherwise identical to version 3.

## 10. Alternatives considered

- **Parent job.** Jobs form a tree; cancel parent cancels children. Rejected: ZZOPS plans have no parent job, and a tree
  forces every owner to create a placeholder job to hold the lease.
- **Reuse `labels.owner`.** It is advisory display text (`model.py`: "NOT authentication"), not unique, not enforced.
- **Cascade only on the ZZOPS side.** That is today's behaviour (remember ids, cancel one by one) and fails exactly when
  the ZZOPS process died. The authority is the only party still alive and trusted to act.
- **Make the lease mandatory.** Would turn a ZZOPS outage longer than the lease into mass cancellation of healthy builds.
  Left to the submitter (ZZOPS config), see its design.
