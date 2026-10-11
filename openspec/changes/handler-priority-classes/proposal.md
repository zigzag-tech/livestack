## Why

2026-10-11, zz-joe (8c/16t, 62 GiB): release jobs `daemon.linux` (16 GiB) and `daemon.windows-gnu` (8 GiB) waited 38 minutes. The per-host memory ledger (`insufficient host memory: claim 8.0 GiB > free -5.4 GiB`) and the CPU reservation (`needs 2, 0 free`) were consumed again and again by e2e jobs that fit whatever room a finishing attempt left. Starvation aging (`starvation_seconds`, 600) eventually helps, but a release build is the user's critical path and should not need ten minutes of aging to beat tests.

A job already carries an integer `priority`, but only submitters can set it, there are four of them in two repositories, and ordering alone does not help: ordering decides who is asked first, not who may take the room a running attempt frees.

## What Changes

- `limits.handler_priority` (authority config): `{handler id: integer 0..1000000}`. A job's effective priority is `max(its own priority, its handler's declared class)`. Submitters change nothing.
- Queue order for non-starved jobs is effective priority, then age. Aged jobs still rank first.
- A job that waits only for free host resources (CPU, memory, disk) reserves them for the round, against jobs of strictly lower effective priority (the starved-job hold mechanism, now also driven by priority). A lower job still starts if it fits in what the held job leaves. Equal priority is not bound. Running attempts are never preempted.
- A lower job refused only because of such a hold reports blocker code `held_for_higher_priority`, naming the holding job.

## Capabilities

### Modified Capabilities
- `workload-placement`: priority classes and priority holds.

## Impact
`workloads/model.py` (Limits), `workloads/placement.py`. Authority config gains an optional key; absent means today's behaviour exactly. The authority reads limits at start only.
