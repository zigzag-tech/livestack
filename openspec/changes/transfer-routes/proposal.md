## Why

LiveStack object movement (inputs, artifacts, uploads, downloads, worker-to-worker) rides
one hard-coded pair of paths: the metered edge relay first, the authority as the only
fallback (`InputTransfer`). When a path is flaky (the China to Canada path resets long
uploads; the relay measured 0.9-1.9 MB/s sequential), the only remedy so far has been
to constrain placement (consumers pin release work to workers near the authority). That
makes the fleet less capable instead of the transport more robust. A failed upload also
restarts from byte 0 because the authority's `PUT objects/<digest>` has no resume.

Direction: LiveStack's transport should become more versatile, general and robust, the
way meshlink picks among paths: try the best route given cost, fail over mid-transfer,
resume at the offset, remember which routes work toward which peer.

## What Changes

- A general `RouteSet` for object transfer: declarative route descriptors (kind, endpoint,
  cost class, priority, budget, constraints) in schema-validated config; pluggable route
  kinds behind one small interface; per-route-per-peer health (EWMA success, throughput,
  latency; circuit breaker with exponential backoff and half-open trial; stale decay);
  cost policy (`expensive` routes never / last resort / allowed); bounded per-route
  concurrency; a route trail for every transfer.
- Resumable chunked upload on the authority (additive): `GET|PUT objects/<digest>/upload`
  with `Content-Range`, per-chunk digest, bounded partial staging. Old authorities answer
  404 and clients fall back to the single-request PUT.
- `InputTransfer` is rebuilt on `RouteSet`; the classic `relay=` arguments produce the same
  two-route behaviour as before (relay first, authority last).
- The edge relay forwards the new upload route and the headers it needs.

Not changed: placement selectors in any consumer; the `mesh://` seam (`transport.dial`);
the edge relay's budget semantics; production rollout (a separate, operator-run step).

## Capabilities

### New Capabilities
- `transfer-routes`: route-set selection, health, cost policy, failover and resume for object transfer.

### Modified Capabilities
- none (workload-upload-grants keeps single-request PUT; chunked grants are a follow-up).

## Impact

`node-py/livestack_node/workloads/{routes,route_kinds,transfer,download,blobs,object_routes,edge_forward,input_cache}.py`,
`node-py/tests/test_workload_routes.py`, `node-py/docs/transport-routes.md`. Workers need a
new release to gain failover; authority and relay need the additive routes before a worker
uses chunked upload (it degrades to the old PUT without them).
