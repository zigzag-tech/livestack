# Design — meshlink transport backbone

## Ownership of durable state

| State | Owner | Notes |
|---|---|---|
| ed25519 attachment key (per node) | the node's `hostd` process | PKCS#8 on disk, mode 0600; public key registered with the relay operator out of band |
| HMAC cap key ring (`bdsr1`) | `relay_control.py` in the broker/coordinator process | mint/verify overlap gives rotation a window; ring file mode 0600 |
| Peer roster (URL-keyed) | `hostbroker.py` (unchanged authority) | mesh peers are URL records like any other; membership states extended, authority not moved |
| Placement / ledger records | `hostbroker.py` + `ledger.py` (unchanged) | placements key on node id; see id boundary below |
| Route-policy candidates | `mesh-route-py` Picker in the caller process | ephemeral; rebuilt from the control plane's target manifest |
| Tunnel attachments | `mesh_outbound_py` in the node process / caller-side mux in MeshPeer | ephemeral by design; durable identity lives in the roster, not the tunnel |
| Relay config (realm, quota, cosmetics) | the relay deploy (meshlink repo) | livestack realm only; benchday realm untouched (DR-1, DR-3, DR-4) |
| `MESHLINK.lock` | livestack repo | one rev covering crate + relay (DR-5) |

## How ids cross the boundary

- **Node identity:** `realm + daemon_id` (DR-2). The operator assigns
  `daemon_id` (e.g. `gpu-box-7`); the node persists it. The attach token
  (`bdrt1`) carries `daemon_id` in its daemon field, signed by the node's
  ed25519 key — so the relay proves *key possession* while the fleet names
  the node by *daemon_id*. Key rotation changes the key, never the id.
- **Peer URL → tunnel route:** `mesh://livestack/<daemon_id>/livestack`
  selects the relay route; the relay mux routes by `<route>/<daemon_id>` door
  path. The roster URL is the single id that planning, membership and the
  dial seam all share.
- **Suffix strips:** the three `/livestack` suffix-strip sites
  (`fleet_admit.py:67-68`, `hostd.py:622-624`, `hostd.py:1093-1096`) become
  scheme-aware so a mesh URL's path segments are not mistaken for a legacy
  suffix.
- **Ledger:** every placement decision on a mesh peer records
  `decision_id` as today, plus `transport=mesh` in the record's join metadata
  so mesh-attributable incidents are queriable without parsing URLs.

## Envelope

One WSS stream per request, `ls-h1` envelope: method, path, headers, body in;
status, headers, body out. The node side terminates via the Python port's HTTP
connector against loopback uvicorn — identical mux bytes to benchday's WS
bridge, only the termination differs. Bearer auth headers ride through
unchanged; the tunnel adds ed25519/HMAC at the relay door, it does not replace
application auth.

## Failure semantics (jidoka)

- Attach failure: report unhealthy, keep serving loopback, retry with backoff
  (1 s → 60 s). Never boot-block.
- Tunnel drop mid-request: the dial surfaces as a failed request, not a hang;
  callers see a named `mesh_tunnel_down` degradation.
- Relay 429 (quota): named `relay_quota` degradation, distinct from node
  unhealth (DR-3).
- Membership: tunnel down → `suspect` (placements kept, fast re-probe);
  only sustained unreachability → `mia` at 600 s. Relay restarts must not
  evict GPU placements.
