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
