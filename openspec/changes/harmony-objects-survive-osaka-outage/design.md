## Context

### Osaka dependency inventory (2026-10-15, no secret values)

| # | What | Where | Key / unit |
|---|---|---|---|
| 1 | Authority reverse tunnel, `ssh -R 127.0.0.1:18810:100.64.0.18:8810` to Osaka :22 | xc-tower-ubuntu `/etc/systemd/system/harmony-edge-tunnel.service`; template in zzops `packages/deployment/edge-units.py` | unit `harmony-edge-tunnel`; key + known_hosts in `~/.config/livestack-workloads/` |
| 2 | Edge relay process (`livestack_node.workloads.edge_forward`), Caddy route `/harmony-relay` | Osaka; `benchday/ops/harmony-edge/{deploy.sh,harmony-edge.service}`, `PUBLIC_URL` and `BW_IP` constants in `deploy.sh` | `harmony-edge.service` |
| 3 | Worker object relay URL | `xc-setup/config/machines/zz-tower0/worker-configs/{unchain-cn,benchday-cn}-zz-tower0.json`, `xc-win-1-wsl/worker-configs/worker{,-2}.json` | `object_relay.url` (+ `key`, `parallel`) |
| 4 | ZZOPS client config on zz-tower0 | `client.json` | object relay URL (the ZZOPS Node client has no code that reads one; see Decision 5) |
| 5 | Authority grant URLs | `authority.json` | `principals[*]` upload base URL (`per-principal-upload-base-url`) |
| 6 | Publisher source upload origin | ZZOPS service entry for Benchday | `sourceUploadOrigin` (`allow-source-grants-through-public-relay`) |
| 7 | GitHub-hosted iOS worker | `benchday/.github/workflows/release-ios-harmony.yml` (`BENCHDAY_HARMONY_AUTHORITY`), `scripts/harmony_github_remote_worker.py` (host and path allowlist pinned to `hs.zztech.io/harmony-relay`) | off-mesh by construction, so a public door is the only path |
| 8 | Headscale control plane (also on Osaka) | `xc-setup/docs/bandwagon-osaka-*.md`, `coordinator-name.md` | no new endpoint exchange while down; see below |
| 9 | DERP region 901 | headscale `derp.yaml` | `jp`, dead; regions 900 (cn) and 902 (na) alive |

Rows 1-7 are the object-delivery paths. Rows 8-9 are why the fallback (mesh) is also poor right now.

### Why the mesh fallback is DERP-only (read-only probes, 2026-10-15)

- `tailscale ping 100.64.0.3` from the authority: five pongs `via DERP(cn)` 252-255 ms, then "direct connection not established".
- The authority's netcheck: UDP works, mapping does **not** vary by destination (easy NAT), public `99.228.40.148`, no IPv6, nearest DERP `na` (28 ms). Its own tailscaled reports it cannot reach the coordination server, so it is running on the last netmap.
- zz-tower0's netcheck: UDP works, public `114.221.255.71`, **`MappingVariesByDestIP: true`** (endpoint-dependent mapping, the hard-NAT class; mesh docs record "tower0 behind China CGNAT"), no IPv6, three STUN-mapped endpoints, nearest DERP `cn` (16 ms). Its tailscaled also cannot reach the coordination server.
- The authority's view of tower0 carries **zero** candidate endpoints (`Addrs` empty). With the control plane down nothing refreshes endpoint advertisements, so disco has nothing to try except what DERP can relay.
- Hard NAT against easy NAT can traverse with birthday-style probing, but Canada-to-Nanjing UDP on this path is what the mesh docs already describe as throttled; the fleet has never recorded a direct path for this pair. Established direct peers (zz-tower2 `119.45.203.144`, zz-joe and the Mac on the LAN) show the machinery works when one side has a public address.
- Conclusion: the blocker is structural (hard NAT on tower0 + trans-Pacific UDP + now no control plane), not a port setting. Fixing it by config is not a reliable plan; the design therefore avoids needing a direct path.

### What meshlink covers today

`~/meshlink` is the connectivity kernel: `mesh-route-core` (route choice, native/WASM/pyo3), `mesh_transport`, and `mesh_relay` with `mesh_outbound`/`mesh_outbound_py` (a NAT'd target attaches *outbound* to a public relay door with `bdrt1` attach and ed25519 challenge, and callers are carried through a tunnel mux). Relay doors are realm-configurable and a deployed second door (HTO) already exists for Benchday. Livestack already binds it: `MESHLINK.lock`, `mesh_attach.py`, `mesh_peer.py`, `relay_control.py`, and the `meshlink-transport-backbone` spec (`mesh://` peers in the same broker as `http://` peers).

Not covered: (a) the Harmony object API is not reached through a mesh door today, only through the Osaka `edge_forward` + SSH tunnel; (b) `transfer-routes` has no `mesh` kind (follow-up 4.4 open); (c) the object relay is livestack's own Python forwarder, so it is not what the "meshlink-based proxy" the laptop agent is building replaces: that work (`xc-setup/docs/mesh-egress-*.md`, gateways on HTO and a commissioned Los Angeles backup) is *egress for hosts to the foreign internet through Xray*, with automatic failover and robustness rollout not yet deployed and tower0 still on HTO. Object delivery is a different data plane and has no failover yet.

## Goals / Non-Goals

**Goals**
- An outage of any one host (Osaka, HTO, Tokyo, tower2) degrades, never stops, large-object delivery for a worker in any region.
- A transfer on a slow path makes progress and resumes, instead of dying on a fixed clock.
- Outages are named where operators look.

**Non-Goals**
- Fixing the NAT traversal pair; changing Headscale/DERP topology; restarting or reconfiguring anything in this change.
- Choosing a CDN or object-store bus vendor (Decision 4 only names the seam).

## Decisions

1. **Doors are an ordered list.** `object_relay` accepts either today's object or a list of doors `{name, url, key_ref, cost}`; the existing object form is a one-door list. Each door becomes a `RouteSet` route of kind `edge_relay`. Worker order is by priority then measured throughput (already `transfer-routes` ordering). Doors in the authority's own region are skipped, as today.
2. **One tunnel per door, authority-initiated, not SSH to one VPS.** `harmony-edge-tunnel` becomes a templated unit instantiated per door. Preferred transport is a meshlink `mesh_outbound` attachment to the door (key-pinned, realm-scoped, reconnects with backoff) so the same door can serve Harmony and Benchday traffic without a shell account; the SSH `ssh -R` form stays a supported transport for a host that runs no meshlink relay. Each tunnel's failure only marks its own route down.
3. **No door shares fate with the control plane.** The Harmony door SHALL NOT be colocated with Headscale. The Osaka box carries Headscale + DERP + Xray; adding bulk traffic there coupled its bandwidth credit to mesh survival. Candidate hosts: HTO (Ashburn, public, nginx already fronts `/benchday-relay`) and tower2 (Nanjing, public, DERP cn) for China-side ingress. A China-side worker reaching an Ashburn door crosses the Pacific on a TLS stream (what the relay measured at 3.8-5.1 MB/s from Nanjing to Osaka); the HTO leg is unmeasured.
4. **A `mesh` route kind and a third, non-relay route.** Register `mesh` (delegates to `mesh-route-py`, reports one outcome) as follow-up 4.4 already intends. Separately keep the seam for an object-store bus route (authority config history shows an `r2` bus-stager rollout; its current state was not verified here) so a bulk object can be staged near the worker. Neither is required to land the door list.
5. **Liveness deadlines.** Replace the ZZOPS fixed 60 s whole-response timer with: a stall deadline (no byte for `stall_seconds`, default 20 s, configurable per route) and an overall ceiling computed as `max(ceiling_min, size / floor_rate)` with `floor_rate` 64 KiB/s by default, so a 2 GiB input cannot hang forever but a 4 MiB block on a 20 KB/s path gets 200 s. The same shape governs livestack `download.py` (it already has per-request `client.timeout` and `max_seconds=3600`; the stall rule replaces the per-request timeout as the abandonment trigger). Where it lives: the ZZOPS Node client for the fixed timer and the missing resume (its `downloadInput` is one un-ranged GET with no retry or relay); livestack `download.py`/`block_codec.py` for the Python worker path. The rule and its constants are specified once (the `object-transfer-liveness` capability) and tested in both.
6. **Adaptive block size.** `CHUNK`/`BLOCK_BYTES` (4 MiB) stay the maximum and the default on fast paths. On a route whose measured throughput over the last window is below `slow_rate` (default 256 KiB/s) the client requests smaller ranges (down to 256 KiB) so each block completes inside the stall window and a reset costs at most one small block; it grows back when throughput recovers. The authority already serves arbitrary ranges; the gzip block codec cap (`BLOCK_BYTES`) is an upper bound only. ZZOPS gains ranged, resumable download using the authority's existing `Range` support (livestack's `download_into(start=)` semantics: prefix re-hashed, digest verified for the whole object).
7. **Loud single-route.** At startup and on each trail, a worker with fewer than two eligible cross-region routes logs and reports `single_route`; a tunnel down, `budget_exhausted` or unreachable door appears in the trail by name (rule 13). A fall-through to the throttled mesh path is flagged `degraded_path: mesh_derp` so a slow transfer is attributable.
8. **Phasing.** Phase A (no new host): liveness + adaptive blocks in both clients, `single_route` reporting; this alone would have turned the 2026-10-15 failure from "impossible" into "slow". Phase B: door list + second door (HTO) + per-door tunnels. Phase C: meshlink tunnel transport and the `mesh` kind.

## Risks / Trade-offs

- HTO is also the egress gateway the laptop agent's meshlink proxy uses for tower0; putting object bulk traffic there couples two features to one host and one provider quota (unknown, recorded as unknown not unlimited). Mitigation: per-door byte budgets (already the relay's model) and Decision 3's rule that doors are separate processes; verify quota before commissioning.
- Stall-based deadlines can keep a trickling transfer alive; the throughput-scaled ceiling and the `floor_rate` bound it.
- The Osaka relay's shared key ring pattern (one key for every door) widens compromise blast radius; each door gets its own `edge_key`.
- Hard-coded `hs.zztech.io` in the iOS GitHub worker must change in step with the door list or iOS releases still die with Osaka.

## Open Questions

1. Which public host should carry the Harmony door: HTO (Ashburn), Tokyo (public hub host), or a new host? Is HTO's bandwidth quota known?
2. Is the Harmony realm to share the Benchday relay door key ring, or get its own realm (design here assumes its own `edge_key` per door at minimum)?
3. Meshlink owner: does the relay (`mesh_relay`) already admit an authority attachment for a non-Benchday realm, and what is the failover state of the egress proxy work that this should not collide with?
4. Should the ZZOPS client move to the livestack worker library (single implementation) rather than gain a second copy of the range/resume/liveness logic in Node?
