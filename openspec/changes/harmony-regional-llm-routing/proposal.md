## Why

Harmony is about to serve LLMs from more than one region (Canada: xc-tower-ubuntu,
zz-whitby; Nanjing next), with callers in both. Canada↔Nanjing is ~250 ms and
GFW-throttled at times, so a request must be resolved and served in its own region
without a synchronous round trip to a broker on the other side of the world.

The pieces exist but are not yet safe to route on across regions:

- **Region policy is applied after warmth.** `fleet_rank.rank()` picks warm before
  cold and `/fleet/rank` filters `regions=` afterwards (`hostd.py` ~601), so a warm
  target in a forbidden region can suppress a cold target in an allowed one.
- **Distance is measured from the wrong place.** `fleet_route.choose()` ranks with the
  default `vantage="direct"` — the fleet broker's own probes, from wherever the broker
  sits (Canada) — not from the caller's region.
- **A routed request can still load.** `x-harmony-routed` stops re-routing, but the
  receiver still admits and loads. A stale route therefore costs a ~50 s swap instead
  of a cheap refusal.
- **The peer shortcut is the wrong tool across hosts.** `_held_elsewhere` probes every
  peer the first-answering broker lists, sequentially, 3 s timeout each, matches by unit
  NAME (not attributes) and accepts a unit that is still loading.
- **Only a speed preference routes across hosts** (`fleet_route.wants_fleet_route`).
  An indifferent request whose entry node has nothing warm loads locally even when a
  warm, in-region unit exists one hop away.
- **The documented negation clause (`{"not": …}`) is not implemented** by the facade's
  matcher (`fleet_embed.attrs_meet`) or the planner; it is compared literally and fails.

The design asked for — "for the next X seconds, requests in this category may go
straight to node N" — is realised here as a **routing view with a TTL plus a target that
validates**, not as a grant that authorises: see design.md.

This realises `_plans/fleet-broker.md` Phase 2 (§4.2 ranking) and
`_plans/region-and-locality.md` for LLM routing. Stale in them: fleet-broker.md assumes
ONE fleet broker; region-and-locality.md's "the caller applies the region filter" is
kept, but the filter must run before readiness, which neither record says. HARMONY.md's
"Only a SPEED preference routes across hosts" becomes false.

## What Changes

Phase 1 — correctness before any cross-region traffic:
- Region eligibility is applied inside ranking, BEFORE warm/cold and finish-time ordering.
- Fleet routing ranks from the caller's vantage (`region:<entry node's region>` by
  default), never from the fleet broker's own position.
- **BREAKING** `x-harmony-routed` gains an expectation. A request routed to a WARM
  target is served resident-only: the receiver never admits, loads, or forwards it; if
  the unit is not resident, not satisfying, or saturated it answers a structured
  `409 route_stale` before any byte of the body. The entry node retries once on the next
  candidate, and never after streaming has begun. A request routed to a COLD target
  (finish-time rule) may load through the receiver's own host broker, as today.
- The same-host peer shortcut is limited to peers on the same host, matched by unit
  attributes, and resident (not loading) on the request path.
- A forward to a same-host peer travels as asked (as fleet forwards already do).
- `{"not": v}` / `{"not": [..]}` is implemented in the one shared matcher used by the
  facade, the ranking and the planner.

Phase 2 — regional routing:
- One fleet broker per region, each building the whole-fleet view; entry nodes read
  their own region's view. Fleet brokers stay observe-only; host brokers stay the sole
  residency authorities.
- Any request whose entry node cannot serve it warm and in policy is ranked over the
  regional view before a local load is considered (not only speed preferences).
- **Router mode**: a harmony-llm node with no units routes every request. Deployed as a
  regional ingress (zz-tower2 for `cn`) and, optionally, on a requester's own machine as
  the "local proxy daemon" — same binary, same contract.
- Routers are deployed on every requester machine at `127.0.0.1:8188` (systemd user
  unit / launchd agent, one rollout script, pinned to the serving nodes' revision); a
  router never registers as a fleet node and holds no credential. Callers are cut over
  to `http://127.0.0.1:8188/v1` one at a time.
- A principal's allowed regions come from the fleet principal table; a request may
  narrow them (`harmony_regions`) but never widen them.
- Partition rule: a cached view may be used past its TTL up to a bounded stale age;
  beyond it the node falls back to today's local-only behaviour. Authorisation is
  always checked at the target, never carried by a view.
- A request id propagates across hops so the entry's route record and the target's
  demand record join.

## Capabilities

### New Capabilities
- `harmony-llm-routing`: how an LLM request is routed across hosts and regions — region
  eligibility, caller vantage, the routed-request contract (resident-only, structured
  stale refusal, bounded retry), router mode, regional views, stale-view bounds, and
  route records.

### Modified Capabilities
<!-- none: harmony-engine-units' requirements (engine, admission, max_concurrent,
     413 re-route) are unchanged; routing is a new capability on top of them. -->

## Impact

- `node-py/livestack_node/fleet_rank.py` — region eligibility inside `rank()`; vantage.
- `node-py/livestack_node/hostd.py` — `/fleet/rank` stops post-filtering regions; fleetd
  per region (config only); principal region policy.
- `node-py/examples/harmony-llm/server.py`, `fleet_route.py` — routed contract, router
  mode, broader routing trigger, peer shortcut narrowing, request id, stale views.
- `node-py/examples/harmony-llm/fleet_embed.py` (`attrs_meet`) and `planner.py`
  (`_unit_satisfies`) — negation, one evaluator.
- Deploy: a `cn` fleetd; routers on zz-tower2 (also the `cn` ingress) and every requester
  machine via `scripts/harmony-router-rollout.sh`; `HARMONY_LLM_FLEET_URL`
  per host. HARMONY.md and benchday `docs/livestack-harmony.md` updated.
- Callers: unchanged (`require:` / `harmony_requires`, OpenAI-compatible); they may point
  `base_url` at their regional ingress or a localhost router.
