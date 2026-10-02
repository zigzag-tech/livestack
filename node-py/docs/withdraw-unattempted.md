# Withdraw: cancel only a job no worker has attempted

`POST /v1/workloads/jobs/<id>/withdraw` (caller role, owner only;
`WorkloadClient.withdraw(job_id)`, `WorkloadStore.withdraw`) ends a job as
`cancelled` (reason `withdrawn by owner before any attempt`) **only** when it is
`queued` with zero attempts. Any other job is returned unchanged; the caller
reads `state` to learn the outcome.

Why it is not `cancel`: `cancel` fences a running attempt and holds its worker
in cleanup. A caller that merely wants a *different input* run instead (Benchday's
test train superseding a job queued on an older snapshot) must not race a claim
with that. The decision runs inside the same `BEGIN IMMEDIATE` transaction
`claim` takes, so exactly one wins: the job is either withdrawn and never
placed, or placed and left alone (`test_withdraw_and_claim_race_has_exactly_one_winner`;
a check-then-cancel version fails it).

A job retried after an infrastructure attempt is not withdrawn: it has an
attempt. An authority older than this route answers 404, which a caller must
treat as "not withdrawn".
