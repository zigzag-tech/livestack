## Context

`WorkloadClient.request` dialled through `transport.dial` (urllib): one TCP
connect per request. The worker issues claim/report/complete on its main
thread and lease renewals on the `harmony-lease` thread, one attempt at a time.
The authority (`WorkloadServer`, `ThreadingHTTPServer` + `BoundedRequests`)
already declared `protocol_version = 'HTTP/1.1'` but closed every response.

## Decisions

### Durable state and ownership
No durable state changes. The connections are process-local: the worker owns
its two control connections (main, lease), the authority owns one handler
thread per accepted connection. Ids crossing the boundary (boot, attempt_id,
fence) are unchanged.

### Two control connections per worker, not one
The main connection is shared by claim/report/complete under a lock. The lease
keeper gets a dedicated channel from `client.channel()` so renewal never queues
behind a long complete or report. The channel is opened by the initial grant in
`LeaseKeeper.start()` (on the caller's thread, before the renewal thread
exists) and used only by the renewal thread afterwards, so it needs no lock
beyond the one the channel already carries. It is closed in `close()`.

With the default 10 s renewal interval and the authority's 15 s idle bound the
lease channel never goes idle long enough to be closed, so an established
attempt makes no new handshakes. The main connection idles out during a long
attempt and reconnects at completion; completion already retries transient
failures for `handoff_retry_seconds`.

### Retry once, only on a reused connection
A kept connection can be closed by the server between requests (idle timeout,
restart). The first write or status read then fails with `RemoteDisconnected`,
`BrokenPipeError`, `ConnectionResetError` or `ConnectionAbortedError` before
any response byte. Exactly that case is retried once on a fresh connection.
A failure on a fresh connection, a timeout, or a failure after response bytes
arrived is not retried here: the caller's existing retry policy
(`retry_transient`, the renewal loop) owns it.

Idempotency of the retry: heartbeat, report, verify-compilation and complete
are idempotent at the authority (complete answers 409 on a replay of a fenced
attempt). Claim is not strictly idempotent, but a claim whose response was
lost is already recoverable: the authority lists the orphan in `cleanup` on the
next report, the path the worker relies on today when a claim response is lost
in flight. The retried case is also the one where the server most likely never
read the request (it closed an idle connection).

### Error surface
`KeptConnection` maps connection-level `OSError`/`http.client.HTTPException`
to `urllib.error.URLError` and HTTP >= 400 to `urllib.error.HTTPError` with a
readable body, matching `transport.dial`, so `lease.transient` and the
client's 4xx/5xx mapping are unchanged.

### Proxy environments
urllib honours `http_proxy`/`https_proxy`. `http.client` does not. When
`urllib.request.getproxies()` names a proxy for the authority's scheme and the
host is not bypassed, `WorkloadClient` keeps the old `transport.dial` path.

### Authority keep-alive and bounds
`Handler.respond` keeps the connection open only when the request body was
fully read (`body()` completed, or the request declared no body) and the
client did not send `Connection: close`; otherwise it sends
`Connection: close` as before. A 401/403 raised before the body was read
therefore still closes, so unread body bytes can never be parsed as the next
request. Object uploads and downloads still close.

Bounds (rule: every resource is bounded with a named enforcer):
- idle: the per-connection socket timeout (15 s, `Handler.setup`) — a kept
  connection that sends no request for 15 s is closed by `handle_one_request`'s
  timeout path.
- count: `WorkloadServer.connection_bound()` = `max_connections` (32, the
  pre-existing bound, now the share kept for short requests: object/CAS
  transfers, verifier, callers) + 2 x worker principals (main + lease kept
  connection each). Recomputed from the live principal set, so a reload that
  adds workers raises it. Ceiling 32 + 2 x 128 = 288. Fleet on 2026-10-02:
  15 worker principals -> 62. Enforced by `BoundedRequests._admit` (a counter
  under a Condition, replacing the fixed semaphore so the bound can follow a
  reload).
- starvation: kept connections are marked idle between requests
  (`Handler.handle_one_request` / `parse_request`). At the bound `_admit`
  shuts down the longest-idle kept connection (logged
  `workload_idle_connection_evicted_at_bound`) and waits up to 1 s for the
  slot; the evicted worker reconnects on its next request through the
  reused-connection retry. Only when every slot is busy is the newcomer
  dropped (`workload_connection_dropped_at_bound`, as before).

## Risks

- Clients that read until EOF instead of honouring `Content-Length` would now
  wait for the 15 s idle close. All in-tree clients (urllib, http.client,
  undici/fetch) honour `Content-Length`.
