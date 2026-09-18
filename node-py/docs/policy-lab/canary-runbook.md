# Future routing-policy canary runbook

This runbook defines a future, separately authorized canary. It does not authorize or
activate one. Offline qualification and shadow recommendations cannot change routing.

## Preconditions

- Bind the candidate, incumbent rollback target, configuration, profile pack,
  calibration domain, evaluator, dataset cutoff, and canary protocol by SHA-256.
- Require an operator-signed release authorization naming those exact hashes, the
  permitted regions/actions, start/end times, traffic ceiling, and rollback target.
- Admit only a calibrated domain. Any missing model, runtime, hardware, workload,
  geography, or interference cell remains excluded.
- Confirm protected service capacity, cleanup ownership, attempt fencing, direct data
  paths, observation health, and an independently reachable rollback operator.

## Isolation and allocation

Use dedicated capacity or randomized whole-region/whole-fleet time blocks. Do not use
per-request A/B assignment on shared GPUs: queues, resident weights, caches, bandwidth,
and batching make those samples interfere. Predeclare block length, at least one warm-up
and one washout interval, maximum requests, maximum duration, resource ceilings, and a
hard end time. Never expand traffic automatically after a quiet window.

## Monitoring windows

Before starting, derive a fixed minimum sample count and window duration from observed
traffic. Monitor per workload and requester region: offered/admitted/good/refused work,
first output, streaming lag/underrun, completion, cancellation cause, queue/load/transfer
time, memory, cleanup, cross-region bytes, fairness, observer gaps, and policy overhead.
Insufficient samples block promotion; they do not prove safety.

Rollback immediately on any invariant violation. Otherwise roll back after two
consecutive adequately sampled windows exceeding the predeclared guard (default: more
than one percentage point SLO-miss regression or more than five percent first-output or
ongoing-lag regression). A human may roll back earlier.

## Rollback semantics

Atomically select the bound incumbent for new work and increment the release epoch.
Do not steal or duplicate ongoing attempts: each retains its original policy epoch,
reservation, route, stream ownership, and cleanup fence until it drains or reaches its
already-authorized cancellation boundary. Accept output/commit only from the owning
fence. Keep cleanup capacity reserved and verify zero duplicate output and zero
post-cancel leakage. Preserve the candidate, rollback release, raw numerators, window
decisions, and authorization as immutable evidence.

## Completion

A fixture test is not a completed canary. Record actual admitted work, interference
controls, monitoring windows, rollback rehearsal/result, operator identity, and exact
artifact hashes before labeling evidence `canary`. Activation still requires a distinct
release authorization after review.
