## Why

On 2026-10-15 the Bandwagon Osaka host (`74.82.195.156`, `hs.zztech.io`) went dark (out of bandwidth credit). Harmony large-object delivery stopped working with it, which is the failure the fleet's own connectivity library (meshlink) exists to prevent. Evidence from that day:

- A 4 MiB CAS range from the Nanjing worker `zz-tower0` to the authority on `xc-tower-ubuntu` failed with `Harmony response deadline exceeded`. The ZZOPS client (`zzops/packages/harmony/harmony-response.mjs`) arms one fixed 60 s timer for the whole response, cleared only when the body closes.
- The mesh path between those two hosts is DERP-only (`tailscale ping`: `via DERP(cn)` ~253 ms, "direct connection not established"). Measured bulk throughput on it is 0.01-0.02 MB/s (`benchday/docs/harmony-source-handoff.md`), so 4 MiB needs 200-400 s; the fixed timer makes every large object impossible rather than slow.
- Every cross-region bypass of that path is Osaka: the worker `object_relay` URL, the authority's `harmony-edge-tunnel.service` (`ssh -R` to Osaka :22), the relay itself, the authority's per-principal grant base URL, ZZOPS's `sourceUploadOrigin`, and the GitHub-hosted iOS worker. All of them fail together with no second route configured.
- Nothing said any of this loudly: workers see the relay as unreachable and drop to the authority over the throttled DERP path, which then "fails" only as a timeout.

Design records this realises: `transfer-routes` (route kinds register; "mesh tunnel, object-store bus, http-pull" were its follow-up 4.4) and the `harmony-transfer-relay` capability in benchday (single fixed upstream relay, which it deliberately tied to one host). Nothing in them is stale; the relay's host identity (Osaka) stops being an assumption of the fleet and becomes a replaceable route.

## What Changes

- **Spec the invariant.** Large-object delivery between any worker region and the authority SHALL NOT depend on any single relay host or on the control-plane host. At least two independently hosted routes SHALL be declared for every worker that is not in the authority's region, and one failure SHALL degrade throughput, not availability.
- **Declare relay doors as data, not as a constant.** Worker `object_relay` becomes an ordered list of doors (URL, key reference, cost class); the authority-side reverse tunnel becomes one tunnel per door, each independent. Candidate doors already operated: HTO/Ashburn (`hto.zztech.io`, `hto-histo-na`), the China door (`tower2.zztech.cc:3389`), Tokyo (`benchday.app`). Which door carries Harmony is a decision, see design.
- **Move the tunnel off SSH-to-Osaka.** The authority attaches outbound to a meshlink relay door as a `mesh_outbound` target (the NAT-traversal role meshlink's relay was built for) instead of `ssh -R` to a single VPS; livestack already carries `mesh_attach.py`, `mesh_peer.py` and `relay_control.py`. A `mesh` route kind (transfer-routes follow-up 4.4) delegates path choice to `mesh-route-py`.
- **Liveness, not a wall clock.** Replace fixed whole-response timers with a stall deadline (no progress for N seconds) plus a throughput-scaled ceiling, and make block size follow measured throughput so a slow path moves smaller, resumable pieces. Applies to the ZZOPS Node client (single un-ranged GET today, no resume, no relay route at all) and livestack `download.py` (fixed 4 MiB `CHUNK`).
- **Say it loudly.** A route that is down, over budget or unconfigured is reported by name in the transfer trail and in the authority/worker health surface; a worker that has only one route in its region reports `single_route` degraded at startup.

Not changed: the authority-authorizes model (digest verification stays mandatory worker-side); control-plane calls (claim, heartbeat, report); Headscale/DERP/Xray topology; the laptop agent's meshlink egress-proxy work (a different product: GFW egress for hosts, not object delivery). No code in this change; this is the proposal.

## Capabilities

### New Capabilities
- `object-delivery-outage-resilience`: no single relay host or control-plane host is on the object-delivery critical path; routes are declared per worker, tunnels are per route, outages are loud.
- `object-transfer-liveness`: stall-based and throughput-scaled deadlines and adaptive block size for object downloads and uploads.

### Modified Capabilities
- none in `openspec/specs` (the base `transfer-routes` change is still unarchived, so this change only adds; once `transfer-routes` archives, the `mesh` route requirement here is read as an addition to it).

## Impact

- livestack: `node-py/livestack_node/workloads/{download,block_codec,route_kinds,transfer,edge_forward,config}.py`, `ops`-level unit templates, `node-py/docs/transport-routes.md`.
- zzops: `packages/harmony/{harmony-response,harmony-download,harmony-http}.mjs`, `packages/deployment/edge-*.py` (tunnel units), service config (`sourceUploadOrigin`).
- benchday: `ops/harmony-edge/*`, `docs/harmony-source-handoff.md`, `.github/workflows/release-ios-harmony.yml`, `scripts/harmony_github_remote_worker.py` (hard-codes `hs.zztech.io`), worker configs under `xc-setup/config/machines/*/worker-configs/` (`object_relay`).
- meshlink: needs the owner's call on a relay door that accepts the authority as a `mesh_outbound` target for the Harmony realm, and a statement of its failover state (see design, open questions).
- Operational rollout (authority and worker releases, unit changes) is a separate operator step and out of scope here.
