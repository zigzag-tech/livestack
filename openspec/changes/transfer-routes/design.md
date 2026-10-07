## Direction (read this first)

The long-term goal is for LiveStack's transport to become more and more versatile, general
and robust. This is NOT a patch for one consumer. Consequences that bind every later change:

- **General.** Nothing in the core names an application, a handler, a host or a region as
  a special case. Regions, sizes and directions appear only as generic descriptor constraints.
- **Pluggable.** A route is a small interface (`Route`: `unavailable`, `upload`, `download`)
  registered under a `kind`. Today: `http`, `edge_relay`. Intended next: meshlink tunnel,
  object-store/CAS bus, LAN/p2p peer, http-pull. Adding one touches no existing kind.
- **Any object movement.** Inputs, artifacts, uploads, downloads, worker-to-worker all go
  through a RouteSet; `InputTransfer` is the first consumer, not the only one.
- **Cost-aware.** Cost is a declared class (free, metered, expensive), enforced by policy,
  never inferred silently. Budgets live where they can be enforced (the relay).
- **Resumable and failing over.** Bytes already moved are never moved again; a route that
  fails mid-transfer hands the remainder to the next route.
- **Loud.** Every abandoned route is logged with its reason and recorded in a trail; if all
  routes fail the error carries the trail. Absence and failure never look alike.
- **Bounded.** Routes per set, trail length, partial uploads (count, age), in-flight
  transfers per route, chunk size are all capped (Benchday rules 10/14 apply to the fleet).
- **Observable and negotiated.** Health is per route per peer; counters are per
  route/outcome; capability is discovered per route (e.g. the resumable-upload probe).

## Context

Route inventory before this change: publisher to authority (direct, or via the edge relay when
`public_base_url`/`upload_base_url` points there); authority to worker and worker to authority
(direct or relay, relay first via `InputTransfer`, 4-way parallel ranged download); worker
mirrors (OSS) for inputs/artifacts; meshlink `mesh://` peers via `MeshPeer` for control and
job traffic (not object bytes). Retry/resume today: downloads resume by range within one
client (`download_into`); uploads are one request, restart from 0.

## Decisions

1. **Plain Python, no new dependency.** pydantic is absent on some worker hosts (7b2e338e).
   Validation is explicit code in `RouteDescriptor.from_config` / `RoutePolicy.from_config`;
   unknown fields refuse.
2. **Ordering = operator intent first, evidence second.** Sort key: not degraded, then
   `priority`, then cost class, then measured throughput, then config order. Priority is
   explicit so the classic order (relay before authority, although the relay is metered)
   is expressible. Health demotes (EWMA success below `degraded_below`) and the circuit
   breaker removes; throughput only separates equals.
3. **Cost policy.** `expensive`: `never` (ineligible, trail says why), `last_resort`
   (default; used only when every non-expensive eligible route has been tried), `allow`
   (ranked by priority like others). Metered routes are ordinary candidates; their own
   budget decides availability (`unavailable()` from `/v1/edge/status`), which is a skip,
   never a health failure.
4. **Do not fork mesh-route-core.** Its Picker scores per-peer paths for meshlink tunnels
   (suspect/demote semantics). Object transfer needs a different unit (route x peer x
   transfer, cost classes, byte budgets). The RouteSet is deliberately the same shape
   (EWMA + demotion + backoff) so a `meshlink` route kind can DELEGATE its internal path
   choice to `mesh-route-py` and report one outcome to the RouteSet. `mesh_route_py` is not
   installed on this host; the python binding is the extension point, not a dependency.
5. **Resume protocol.** `GET objects/<d>/upload` returns `{offset, size, complete}`;
   `PUT objects/<d>/upload` with `Content-Range: bytes a-b/total` and `X-Chunk-Digest`
   appends only at the staged end. A mismatch answers 409 with the authority's offset so a
   lost acknowledgement resynchronises rather than resends. The final chunk verifies the
   whole digest and commits. State is held by the authority, so ANY route to it resumes.
   Partials: one private file per (owner, digest), at most 16, 1 h idle, discarded on restart.
6. **Download failover** continues at `out.tell()`; the prefix is re-hashed from disk, so
   the final digest still covers bytes fetched through different routes. A 409 (unverifiable
   bytes) makes the next route restart the object.
7. **Receipts.** `InputTransfer.last_trail` / `recent_trails` and `routes.counters` carry the
   trail. Putting it in the worker completion receipt needs an authority schema change and is
   a follow-up task, not forced into the artifact dict here.

## Placement

When uploads and downloads from far workers are reliable, consumers can drop route-avoiding
selectors (e.g. region pins). This change changes no selector; the evidence (trails, counters,
resumed-transfer rate) is what justifies each such change per consumer.

## Risks

- A route whose resumable probe passes but whose chunk PUT is rewritten by a middlebox: the
  chunk digest catches corruption; the legacy single PUT remains the fallback per route.
- Partial files consume disk until swept (bounded by count and 1 h idle).
- Health is per `InputTransfer` instance (workers build several); sharing one RouteSet per
  process is a follow-up.
