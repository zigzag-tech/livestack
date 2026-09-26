# meshlink-transport-backbone Specification

## Purpose
How a fleet node with no reachable IP joins the fleet, stays joined, and is
dialed by brokers and callers — through the meshlink relay as the single
connectivity backbone, alongside (never instead of) plain-HTTP peers on
reachable networks.

The backbone is bound, not forked: route policy is the Rust `mesh-route-core`
via the `mesh-route-py` binding; the relay and the wire mux are meshlink's,
conformance-pinned by `MESHLINK.lock`. A peer remains an opaque URL string in
every roster — `http(s)://` and `mesh://` peers interoperate in one broker.

## ADDED Requirements

### Requirement: Peers are scheme-selected, transport is a seam

`make_peer` / `build_broker` SHALL select the peer implementation by URL
scheme (`http(s)://` → RestPeer, `mesh://` → MeshPeer), and every inter-node
dial SHALL go through the `transport.dial` seam with the urllib default
behavior moved, not rewritten.

#### Scenario: Mixed roster in one broker
- **WHEN** a broker's roster holds one `http://` peer and one `mesh://` peer
- **THEN** planning, membership and dispatch treat both as ordinary URL-keyed
  peers and both serve requests

### Requirement: Mesh identity survives key rotation

A mesh-attached node's identity SHALL be `realm + daemon_id`, not its ed25519
key; rotating the key SHALL NOT change node id, placements, or ledger history.

#### Scenario: Rotation mid-membership
- **WHEN** a node's attachment key is rotated and it re-attaches
- **THEN** the broker observes the same node id, keeps its placements, and
  in-flight tunnels survive on the ring's mint/verify overlap

### Requirement: Tunnel loss is transient until proven otherwise

A dropped tunnel SHALL demote a mesh peer to `suspect` (placements kept, fast
re-probe), and only sustained unreachability (mia at 600 s) SHALL drop
placements; a relay restart SHALL NOT trigger unit evictions.

#### Scenario: Relay restart
- **WHEN** the relay restarts while GPU placements are held on mesh peers
- **THEN** peers enter `suspect`, re-attach on recovery, and no placement is
  evicted

### Requirement: Failures are named, never silent

An attach failure SHALL report unhealthy without boot-blocking; a dial over a
dead tunnel SHALL surface as a named `mesh_tunnel_down` degradation; a relay
quota refusal SHALL surface as `relay_quota` — distinct from node unhealth.

#### Scenario: Attach failure at boot
- **WHEN** a node boots and the relay is unreachable
- **THEN** the node serves loopback, reports unhealthy on the health surface,
  and retries with backoff (1 s → 60 s)

### Requirement: Conformance is pinned

Livestack SHALL carry a `MESHLINK.lock` covering both the `mesh-route-py`
crate and the relay version, and a check script SHALL fail CI on drift.

#### Scenario: Drift detected
- **WHEN** the checked-out meshlink rev differs from `MESHLINK.lock`
- **THEN** the check script exits non-zero with the expected and actual revs
