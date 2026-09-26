# meshlink transport backbone

## Why

Livestack today assumes every daemon, coordinator and broker is reachable on
one LAN/VPN: every inter-node dial is plain urllib HTTP to a reachable IP
(~20 dial sites; see the seam survey in `_plans/meshlink-backbone-plan.md`
Phase 4). That assumption is why fleet nodes need inbound ports or a VPN.
meshlink (`github.com/zigzag-tech/meshlink`) is the extracted benchday
connectivity kernel — outbound-only WSS attach, ed25519/HMAC token auth, one
Rust route-policy core — and the 2026-09-21 ruling (`benchday/docs/mesh-route.md`)
says Livestack binds it rather than reinventing it. This change makes meshlink
the connectivity backbone: a fleet node with only outbound 443 is fully
operational.

The design record is `_plans/meshlink-backbone-plan.md`; the five binding
decisions (realm, identity, quota, cosmetics, pinning) are in
`_plans/meshlink-backbone-decisions.md`. What is stale without this change:
every `_plans/*.md` doc that says "reachable IP" as a precondition for node↔
broker communication.

## What Changes

- A transport dial seam (`livestack_node/transport.py`): `dial(target, …)` —
  the current urllib behavior moved, not rewritten, as the default impl.
- `MeshPeer` over the meshlink stack: `mesh://livestack/<daemon_id>/livestack`
  peers ride WSS tunnels through the relay, HTTP request/response enveloped
  per stream (`ls-h1` envelope), one stream per request.
- Scheme-aware peer construction (`mesh://` → MeshPeer, `http(s)://` →
  RestPeer) in `make_peer` / `build_broker`; `/livestack` suffix strips become
  scheme-aware. Peers stay opaque URL-keyed records — planning, membership,
  pruning untouched.
- Announce path: nodes attach outbound after the local facade serves,
  advertise their `mesh://` target, and a failed attach reports unhealthy
  without boot-blocking.
- `relay_control.py`: mints `bdrt1` attachments / `bdsr1` caps for the
  livestack realm (DR-1), renewal and key rotation via mint/verify overlap.
- Liveness: tunnel-down is `suspect` (keep placements, fast re-probe), not
  `mia`; a relay restart never triggers unit evictions.
- `MESHLINK.lock` + drift check (DR-5).

## Capabilities

- `meshlink-transport-backbone` (new): how a fleet node with no reachable IP
  joins, stays joined, and is dialed by brokers/callers through the meshlink
  relay; how identity survives key rotation; how tunnel loss maps to
  membership states.

## Impact

`node-py/livestack_node/` gains `transport.py`, `mesh_peer.py`,
`relay_control.py`; `announce.py`, `hostbroker.py`, `hostd.py`, `client.py`,
`serve.py`, and the workloads/perception/policy_lab dial sites re-point
through the seam. Listeners unchanged — tunnels terminate on loopback uvicorn.
New isolated e2e lane (relay + broker + node in a no-inbound netns). External
dependency: meshlink `mesh-route-py` crate and `mesh_outbound_py` package
(meshlink change `python-connectivity-consumer`), pinned by `MESHLINK.lock`.
