# Transport routes

How LiveStack moves object bytes (inputs, artifacts, uploads, downloads, worker to worker)
when more than one path exists. Spec: `openspec/specs/transfer-routes` (change
`transfer-routes`). Code: `node-py/livestack_node/workloads/{routes,route_kinds,transfer}.py`.

## Principles

LiveStack's transport should become more versatile, general and robust over time. It is not
a patch for one consumer; keep application, handler, host and region names out of the core.

1. **Versatile**: a route is a small interface registered under a kind. New ways to move bytes
   (mesh tunnel, object-store bus, LAN peer, http-pull) are new kinds, not edits to old ones.
2. **General**: any object movement uses a `RouteSet`; `InputTransfer` is just the first user.
3. **Robust**: if a route fails, try another; resume, never restart; remember what works
   toward which peer (health is per route per peer, with a circuit breaker).
4. **Cost-aware**: routes declare `free | metered | expensive`; policy decides whether an
   expensive route is `never`, a `last_resort` (default) or `allow`ed. Budgets are enforced
   where the bytes pass (the relay) and surface as "unavailable", not as failures.
5. **Resumable**: downloads continue at the saved offset; uploads at the offset the
   authority holds; every chunk and the whole object are digest-verified.
6. **Loud**: each abandoned route is logged with its reason and kept in the transfer's
   trail (`InputTransfer.last_trail`); total failure raises with `route_trail`.
7. **Bounded**: routes per set, trail length, in-flight per route, partial uploads, chunk size.

## Configuring routes

```python
RouteSet.from_config({
  'policy': {'expensive': 'last_resort', 'failure_threshold': 3, 'open_seconds': 5},
  'routes': [
    {'name': 'relay', 'kind': 'edge_relay', 'endpoint': 'https://...', 'cost': 'metered', 'priority': 0},
    {'name': 'authority', 'kind': 'http', 'endpoint': 'https://...', 'cost': 'free', 'priority': 1,
     'constraints': {'directions': ['get', 'put'], 'max_bytes': 2147483648}},
  ]}, client=..., ...)   # context kwargs go to the kind's factory; credentials never live in descriptors
```
Unknown fields are refused. Ordering: not degraded, then `priority`, then cost class, then
measured throughput, then config order.

## Liveness: stall window, ceiling, adaptive blocks

Object downloads (`download.py`, constants in `liveness.py`) are abandoned by liveness, never by a
fixed whole-response clock:

| Rule | Default |
|---|---|
| Stall: no byte for | 20 s (`stall_seconds`; this is the per-read socket timeout) |
| Request ceiling | `max(60 s, nbytes / 64 KiB/s)` (`ceiling_minimum`, `floor_rate`); abandoned as `duration` |
| Object ceiling | `max(max_seconds = 3600 s, size / floor_rate)` |
| Block size | 4 MiB max, 256 KiB min (`max_block`, `min_block`) |
| Next block | EWMA (alpha 0.5) of the last full block's goodput: at or above `slow_rate` (256 KiB/s) double toward max; below it, what the path moves in half a stall window, floored to 64 KiB, clamped; any failed attempt halves |

Per-route overrides go in a descriptor's `options` (the same names). The resume offset is the
bytes already written, so a stall or ceiling costs the bytes in flight, not the object; a failure
still counts against `max_failures`. The trail event carries `block_bytes`, `min_block_bytes`,
`stalls`, `ceilings`, `rate_bps`, and `degraded_path` when a route declared with
`options={'degraded_path': 'mesh_derp'}` carried a transfer after another route was skipped or
failed. `InputTransfer.health()` reports `single_route` for a cross-region worker with fewer than
two non-direct routes (also logged at construction).

The ZZOPS Node client cannot import this module, so the rule is a contract: both sides run
`tests/fixtures/transfer_liveness_vectors.json` (zzops keeps a copy). Change the vectors first.
Parallel fan-out (`parallel` > 1) still uses fixed 4 MiB blocks, with the stall window and
request ceiling applied; a failed block falls back to the adaptive sequential loop.

## Adding a route kind

1. Subclass `routes.Route`; implement `upload(source, digest, size, ctx)` and/or
   `download(digest, out, ctx)`. Raise on failure; return only after bytes are verified.
   Add `unavailable()` if the route can know cheaply that it should not be used (budget, link down).
2. Honour resume: downloads start at `out.tell()` (use `download_into(..., start=)` when the
   endpoint speaks the authority's object API); uploads ask the endpoint where it is.
3. Set `ctx['moved']` to the bytes you moved so health and the trail are truthful.
4. `register_kind('name', factory)`; add a test with a REAL local server that injects resets,
   stalls, 5xx and slowness (see `tests/test_workload_routes.py`).

A mesh kind should delegate its internal path choice to `mesh-route-py` and report a single
outcome to the RouteSet; do not copy the scorer.
