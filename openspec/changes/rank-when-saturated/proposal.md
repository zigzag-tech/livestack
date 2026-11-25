## Why

The fleet runs one model (Inkling-2-Mini) as four declared units on three nodes, and only
node-g keeps it loaded. Every llm request and every Simple Jev decision in the fleet
queued behind node-g's 2 slots, one of them often a ~60 s reasoning call, while the same
model sat unloaded on node-h (measured load 4 s, 10 samples) and on the tower's second
card. Measured 2026-11-25: a one-token Jev decision waited 32 s on node-g, and a request
entering node-h was forwarded there (`fleet route (no_warm_local) -> 100.64.239.1`).

Two gaps keep it that way:

1. `_local_ready` counts a resident unit as ready however full it is, so a saturated node
   never asks the fleet. The current spec already says "no resident, **unsaturated** unit";
   the code does not check saturation.
2. `fleet_rank.rank` compares a cold copy against a warm one by finish time only when the
   request states a speed preference. Indifferent requests (`require:class=llm`, what the
   hub's Jev and the Overlord send) always stay warm-first.

**Design records realised:** `_plans/route-selection.md` (the finish-time rule),
`_plans/fleet-broker.md` §1 (host brokers stay the residency authorities; a cold-routed
request loads through the target's own broker). **Stale:** route-selection.md describes the
finish-time rule as speed-preference only.

## What Changes

- A resident unit with no free slot is `saturated`: the entry node ranks the request over
  the fleet (trigger `saturated`) instead of queueing without asking.
- When any warm candidate is saturated, every target is priced by finish time (queue wait +
  measured load if cold + first token + tokens / decode rate), with or without a speed
  preference, and ordered by it. A cold copy wins only when it finishes sooner; unpriced
  targets keep the old rule.
- Per-unit inventory is used for pricing without a `prefer` (equal preference: the
  resident unit).

No new configuration, no pin: the second copy loads through its own broker as any
cold-routed request does, and the existing idle unload removes it.

## Impact

- `node-py/examples/harmony-llm/server.py` (`_local_ready`, `_local_saturated`),
  `fleet_route.py` (`wants_fleet_route` names `saturated`), `livestack_node/fleet_rank.py`.
- Every request through a saturated node now pays one cached fleet-view ranking.
