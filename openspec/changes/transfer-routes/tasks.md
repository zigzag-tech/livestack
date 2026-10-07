## 1. Core
- [x] 1.1 `routes.py`: RouteDescriptor/RoutePolicy validation, Health + circuit breaker, Trail, Route interface, kind registry, RouteSet ordering
- [x] 1.2 `route_kinds.py`: `http` and `edge_relay` kinds (resumable chunked upload, resumed download, relay status gate)
- [x] 1.3 `transfer.py`: InputTransfer on RouteSet, classic relay arguments preserved, trail and counters exposed
- [x] 1.4 `download.py`: resume at a saved offset (prefix re-hashed)

## 2. Authority and relay (additive)
- [x] 2.1 `blobs.py` `put_range` / `upload_offset` / `completed_size`, bounded partials, per-chunk digest
- [x] 2.2 `object_routes.py` `GET|PUT objects/<digest>/upload`
- [x] 2.3 `edge_forward.py` forwards the upload route, `Content-Range`, `X-Chunk-Digest`

## 3. Tests (real sockets, injected faults)
- [x] 3.1 mid-body connection reset, stall, 5xx, truncated download, lost ack, budget exhausted
- [x] 3.2 resume at offset on the alternate route, no duplicate bytes, digest verified
- [x] 3.3 circuit open / half-open / close / backoff cap / stale decay; cost policy; saturation; trail logged

## 4. Follow-ups (not in this change)
- [ ] 4.1 worker config `routes` block (schema-validated) and one shared RouteSet per worker process
- [ ] 4.2 carry the route trail in the completion receipt (authority schema)
- [ ] 4.3 chunked upload through upload grants
- [ ] 4.4 route kinds: meshlink tunnel (delegating to mesh-route-py), object-store bus, http-pull
- [ ] 4.5 rollout: authority + relay first, then worker release; consumers revisit placement pins
