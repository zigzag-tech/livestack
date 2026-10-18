## Context

See proposal.md for why. What exists on `main` (2026-10-18) that this builds on:

- `fleet_route.py`: an entry harmony-llm node fetches the fleet broker's `/fleet` view
  (cached 10 s), ranks it with `fleet_rank.rank()`, and forwards to the winner with
  `x-harmony-routed` set. Triggered only by a speed preference.
- `fleet_rank.rank()`: requirement filter, warm-for-requirement, finish-time pricing of
  cold targets, preference, distance band, load. Supports `region:<r>` vantages
  (median of that region's measured links). Region policy (`regions=`) is applied by
  `hostd`'s `/fleet/rank` AFTER `rank()`.
- Host brokers (`livestack-hostd`) are the sole residency authorities; the fleet broker
  (`livestack-fleetd`) is observe-only and authenticates `/fleet/admit`.
- Meshlink owns client → endpoint PATH selection (direct / relay / P2P). Harmony owns
  WHICH node serves. This change does not touch the former (`_plans/route-selection.md`).

## Goals / Non-Goals

**Goals:**
- A request entering in region R is resolved and served in R without a synchronous call
  outside R, whenever R can serve it.
- A stale routing decision costs at most one cheap refusal and one retry — never a load.
- One binary covers GPU node, regional ingress and requester-local proxy.

**Non-Goals:**
- Fleet-wide owner fairness or quotas at the target queue. Today's per-unit bounded queue
  stays; routing does not make this worse than today (every request already reaches the
  target directly), and fair queueing is its own change.
- Authenticating `/v1` on the mesh. The trust model is unchanged; routing must not widen
  it (views carry no credentials; headers pass through untouched).
- Path selection over the network (Meshlink's).
- Cross-region quota partitioning that survives a partition.

## Decisions

**1. The "grant" is a routing view with a TTL, validated at the target — not a
per-category authorisation.** The original idea was central control issuing "for X s,
category C may go straight to node N". A per-category grant cannot carry preferences
(two requests a grant covers may prefer different units) and needs revocation to be
safe. Instead the entry node holds the whole regional view (already what `fleet_route`
caches) and runs `rank()` per request — "category" is just the existing requirement
grammar evaluated locally, in microseconds. Safety comes from the target, which refuses
a warm-routed request it cannot serve resident-only (spec: resident-only). So a stale
view is harmless by construction and needs no revocation channel.
*Alternative*: signed grants with epochs and revocation — rejected: more state, and the
target must still validate, at which point the grant adds nothing.

**2. Expectation on the routed header.** `x-harmony-routed: <router-id>;expect=warm|cold`.
`warm` → resident-only path (no admit/load/forward). `cold` → the finish-time rule
decided a load is worth it, so the receiver's host broker admits as for a local request.
Without the expectation the receiver cannot tell a stale warm route (must refuse) from a
deliberate cold one (must load). Receivers treat a header with no `expect` as `cold`
(today's behaviour) — every node is redeployed together, so this is only defensive.

**3. Region eligibility moves into `rank()`.** `rank(..., allow_regions=)` filters before
building warm/cold sets. `/fleet/rank` passes `regions=` in rather than post-filtering.
The invariant from region-and-locality.md — "ranking without a restriction ignores
region" — still holds: with no `allow_regions` nothing is filtered.

**4. Vantage = entry node's region.** `fleet_route.choose()` passes
`vantage=f"region:{LIVESTACK_NODE_REGION}"`. When the region has no measured links
(new region), `distance_to` returns None and distance stops ordering — acceptable, and
recorded. A router-mode node on a requester's machine uses its own region the same way.

**5. One fleet broker per region, same code.** Each fleetd builds the whole-fleet view
from the same membership and its own probes. Entry nodes read `HARMONY_LLM_FLEET_URL`
for their region. There is no "central" vs "regional" code path: the cn broker is a
second deployment. Policy that must be the same everywhere (principal region
allowances) lives in the fleet principal table, distributed by the existing provisioning
path; a node reading an older table can only be stricter or looser by one revision, and
the target re-checks.
*Alternative*: one central broker issuing delegated grants to regional brokers —
rejected: that is the synchronous cross-region dependency this change removes.

**6. Routing trigger widens from "speed preference" to "no warm local option".** Today an
indifferent request on a node with nothing resident loads locally (~50 s) even with a
warm peer in-region. Ranking first lets warm-first win; a local load still happens when
the ranking picks this node. Speed preferences keep routing even when a local unit is
warm (unchanged).

**7. Router mode = a facade with zero units.** No new daemon. Zero `SPECS` means every
request takes the routing branch; `/harmony/route` already exists for dry runs. The
"proxy daemon on the requester's machine" is this, run on localhost with the region set.

**8. Peer shortcut narrows to same host.** `_held_elsewhere` keeps "resident or loading"
for warm-on-start (its original purpose — no double load on one host) but the request
path asks for same-host, attribute-satisfying, resident peers only. Cross-host deferral
is the ranking's job; per-request sequential 3 s probes across a 250 ms link are not.

**9. Negation in one evaluator.** `attrs_meet` gains `{"not": …}`; the planner's
`_unit_satisfies` and `fleet_rank`'s matcher call the same function (moved to
`livestack_node`, imported by the facade).

### Durable state — who owns it, how ids cross

| State | Owner process | Crosses as |
|---|---|---|
| Residency, admission, eviction | host broker (`livestack-hostd`), per host | `admit()` grant; never by a view |
| Fleet view (nodes, units, attributes, evidence, region, links) | regional fleet broker (`livestack-fleetd`), per region | `GET /fleet` snapshot with `generated_at`; cached by entry nodes, never persisted |
| Principal region allowance | fleet principal table (`/etc/harmony/fleet-principals.json`), provisioned per host | principal resolved at the entry from the caller's token; region list on the route record |
| Route record | entry/router node's demand log (`fleet_route` field) | `x-request-id` header, unchanged across hops |
| Demand record of the served request | target node's demand log | same `x-request-id`, plus `x-harmony-routed` router id |

Ledger: the route record on the entry's demand log is the routing decision's ledger row
(as `fleet_route` is today); `/fleet/rank` keeps its existing ledger rows.

## Risks / Trade-offs

- [Two regional brokers disagree] → the target validates; disagreement costs a
  `route_stale` and one retry. Route records carry `view_generated_at` and the broker,
  so disagreement is visible.
- [Retry storms when a popular unit evicts] → one retry per request, then fail with
  reasons; the router drops a target that answered `route_stale/not_resident` from its
  cached view until the next refresh.
- [Wider routing trigger changes behaviour for indifferent requests] → only when nothing
  local is warm; previously those requests paid a local load. Verify on the tower +
  whitby pair before cn exists.
- [Localhost routers multiply load on fleetd] → views are cached per TTL; N routers is N
  fetches per TTL, the same order as today's entry nodes. Revisit if routers exceed tens.
- [Stale view during a partition routes to a dead target] → connection failure before
  any byte counts for the one retry; bounded stale age then falls back to local.
- [No fleet-wide fairness] → accepted as a non-goal; noted in HARMONY.md.

## Deployment

Router mode imports no engine (vLLM and Strata are child processes, never imported), so a
router needs only `fastapi`, `uvicorn`, `httpx` and `livestack_node`, and runs on Linux,
WSL and macOS without a GPU.

| Decision | Choice |
|---|---|
| Where | Every requester machine: zz-tower2 (hub; also the `cn` ingress), zz-tower0, xc-mac-studio, lappy, xc-win-1 WSL. GPU hosts (xc-tower-ubuntu, zz-whitby) run none — their serving node already routes. |
| Address | `127.0.0.1:8188` on every machine — the port GPU hosts' serving node already uses — so every caller's `base_url` is `http://127.0.0.1:8188/v1` everywhere and no caller names a host. |
| Bind | Loopback on requester machines. Only the zz-tower2 ingress also binds its mesh IP. |
| Config | `LIVESTACK_NODE_REGION` (`na`/`cn`) and `HARMONY_LLM_FLEET_URL` (that region's fleetd), in one env file per machine (`~/.config/harmony-router/env`); units file absent ⇒ router mode. |
| Supervisor | systemd **user** unit on Linux/WSL (`loginctl enable-linger`), launchd agent on macOS; `Restart=always`. Unit file and plist committed under `node-py/examples/harmony-llm/systemd/router/`. |
| Code and venv | Runs from the machine's `~/livestack` checkout with its own venv (`~/.local/share/harmony-router/venv`, the `facade` extra plus `uvicorn`). |
| Version | Router and serving nodes share the routing contract version (spec). The rollout installs the revision the GPU nodes run, read from their `/health`, rather than whatever `~/livestack` holds; repo-sync updating `~/livestack` does not restart routers. |
| Credentials | None held. Caller headers pass through (spec). |
| Router down | Callers fail fast; the supervisor restarts it. Callers MAY list their regional ingress as a second `base_url`; no fallback logic is added to routers. |
| Rollout | `scripts/harmony-router-rollout.sh [host…]` (modelled on xc-setup's `diskreap-rollout.sh`): ssh, check out the pinned revision into a dedicated worktree, build the venv, install env + unit, start, then a `/harmony/route` dry run against `127.0.0.1:8188` that must name a target. `--status` prints each host's router version, region and last dry run. Idempotent. |
| Caller cutover | One caller per change, each in its own repo: benchday hub (`llmConfigBridge.ts`, `narration/summarize.ts`), media-corpus ingest, jingway catalog route. Each cutover is verified by its demand records arriving at a target with a router id. |

**Why the same port everywhere.** A caller that runs on a GPU host and on a laptop gets
the same `base_url`, and moving a caller between machines needs no config change. The
cost is that a router cannot run on a GPU host — which it does not need to.

**Why routers do not follow repo-sync.** The routed contract is between router and
target. A router that silently upgrades ahead of the GPU nodes (or lags them) breaks the
contract fleet-wide on the next sync; pinning to the serving nodes' revision makes the
GPU redeploy the single upgrade event, followed by `harmony-router-rollout.sh`.

## Migration Plan

1. Land Phase 1 (eligibility, vantage, routed contract, peer narrowing, forward as asked,
   negation) and redeploy every harmony-llm node and fleetd together (tower, whitby).
2. Verify on the existing two-host `na` fleet with `/harmony/route` dry runs and a forced
   stale route (evict a unit, send a warm-routed request → 409, retry lands elsewhere).
3. Phase 2: deploy a `cn` fleetd and the zz-tower2 router (ingress); set
   `HARMONY_LLM_FLEET_URL` on cn nodes; add principal region allowances.
4. Roll routers out to requester machines (Deployment), then cut callers over one at a
   time.
5. Rollback: redeploy the previous node release; the routed header without `expect` is
   read as today's behaviour, so mixed fleets during rollback degrade to today's.

## Open Questions

- Whether xc-win-1's Windows side (not WSL) needs a router — no Windows-native caller is known today.
- Maximum stale view age: default 300 s (30× the 10 s TTL) — tune from partition data.
- Whether the router should also cache per-target `route_stale` refusals beyond one
  refresh (negative caching) — decide from the route-record data after Phase 1.
