## Why

On 2026-10-01 the Lima guest `xc-mac-studio-harmony` lost three attempts
(30e1c66a, 463d9ab6, db535438) to lease expiry while the authority was healthy.
The guest reaches the world through Lima's user-mode network
(gvisor-tap-vsock), which admits at most 10 in-flight outbound TCP handshakes
and holds each one until the Mac's own connect gives up (~75 s). An e2e hub in
the guest dialled unreachable addresses and filled all 10 slots; every lease
renewal opened a NEW connection (urllib, one TCP connect per request), queued
behind them, and timed out until the 120 s lease ran out.

A renewal that needs a fresh TCP handshake each time inherits every fault of
the host's connect path. A renewal that rides a connection already established
does not: it needs only the path for bytes on an open flow.

Realises `_plans/durable-workloads.md` (worker-owned renewal, monotonic
deadline). That record says nothing about connection reuse; it is silent rather
than stale. The guest's network itself is fixed separately (benchday,
`docs/harmony-worker-enrolment.md`, vzNAT); this change is the general,
host-independent half.

## What Changes

- `transport.KeptConnection`: one persistent HTTP/1.1 connection (stdlib
  `http.client`) behind the fleet dial seam, with urllib's error surface
  (`HTTPError` on >= 400, `URLError` on connection failure) so call sites keep
  their `except` clauses. A request that fails on a REUSED connection before
  any response byte arrives is retried once on a fresh connection; nothing
  else is retried there.
- `WorkloadClient.request` rides one kept connection (lock-serialised). The
  `LeaseKeeper` takes its own channel (`client.channel()`), opened for the
  initial grant, so a renewal never waits behind a claim/report/complete on the
  main connection and never needs a new handshake while the attempt runs.
  Object/CAS transfers keep using per-request connections (`transport.dial`).
- Where urllib would have routed the authority through an environment proxy,
  the client keeps the old per-request path unchanged.
- Authority: responses to control requests keep the connection open (HTTP/1.1,
  `Content-Length` on every response) when the request body was fully
  consumed and the client did not ask to close; anything else still closes.
  Object streams still close. The existing 15 s per-connection socket timeout
  is the idle bound. The existing connection bound (32, livestack 06be1c0c)
  becomes `32 + 2 x worker principals` (each worker keeps at most two
  connections), recomputed on principal reload; at the bound the longest-idle
  kept connection is closed to admit a newcomer, so idle workers can never
  starve object/CAS, verifier or caller requests.

## Impact

- `node-py/livestack_node/transport.py`, `workloads/client.py`,
  `workloads/lease.py`, `workloads/http.py`, `workloads/network.py`.
- Wire-compatible both ways: a new worker against an old authority gets
  `Connection: close` and reconnects per request (old behaviour); an old worker
  against a new authority closes its own urllib connections.
- Through the edge forwarder (`edge_forward.py`, always `Connection: close`) the
  worker falls back to a connection per request, as today.
