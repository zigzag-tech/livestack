# Meshlink backbone — staged rollout runbook

**Status: EXECUTED 2026-09-26/27** (production; the runbook below is retained
as the procedure record). Per-host end state is appended at the bottom under
"Executed — 2026-09-26". The week-long observation windows are operator time; nothing
in this document has been performed against real hosts yet.

**Prerequisites (all landed, green on origin/main):**

- meshlink: pyo3 route core, `mesh_outbound_py`, realm-configurable relay
  cosmetics (`realm_door_e2e`), all on origin/main ≥ `c6ab96f`.
- livestack: transport dial seam, MeshPeer, announce path, relay control plane,
  liveness mitigations, the consolidated no-inbound e2e lane
  (`node-py/tests/test_mesh_e2e.py`), at ≥ the Phase 9 commit.
- A relay deployment answering for the `livestack` realm, with the realm's
  door cosmetics, attachment trust key (pub) and cap-key ring configured.
  Livestack's `MESHLINK.lock` pins the relay build both repos conform against.
- Fleet quota set at relay deploy time per DR-3 (livestack fan-out exceeds the
  relay default of 4 streams / 240 req-min); it stacks under
  `LIVESTACK_ACCOUNT_QUOTA`.

**Rollback for every step, at all times:** a node's mesh identity is additive
to its HTTP identity. Reverting is `LIVESTACK_MESH_ENABLED=0` (or removing the
daemon-id-shaped `LIVESTACK_NODE_HOST`) + restarting the node — it re-announces
its `http://` URL on the next register cycle and the broker probes it exactly
as before. HTTP peers are never deprecated; mixed rosters are the tested
steady state (`tests/test_mixed_roster.py`).

---

## Step 0 — staging: one xc worker attaches alongside its HTTP identity

Host: one staging xc worker (recommend `xc-win-1`-class or an idle xc worker;
NOT a production inference host). Operator: whoever owns that host's systemd
units, with the relay operator in reach.

1. **Relay-side** (relay operator): add the worker's attachment pubkey to the
   `livestack` realm record; ensure the realm cap-key ring is the same ring the
   fleet broker will mint from.
2. **Node-side** (host operator), in the node's systemd unit or equivalent:
   ```
   LIVESTACK_MESH_ENABLED=1
   LIVESTACK_MESH_DAEMON_ID=xc-staging-1        # stable, operator-assigned (DR-2)
   LIVESTACK_MESH_DAEMON_KEY_FILE=/etc/livestack/mesh-daemon.key   # 0600
   LIVESTACK_RELAY_URLS=wss://<relay>:443
   LIVESTACK_RELAY_IDS={"wss://<relay>:443":"<relay-id>"}
   LIVESTACK_RELAY_KEY_FILE=/etc/livestack/mesh-mint.key           # 0600, mints bdrt1
   ```
   Restart the node.
3. **Broker-side** (fleet broker operator):
   ```
   LIVESTACK_RELAY_URLS=wss://<relay>:443
   LIVESTACK_RELAY_IDS={"wss://<relay>:443":"<relay-id>"}
   LIVESTACK_RELAY_CAP_KEYS=[{"kid":"k1","secret":"…","active":true}]
   ```
   Restart the fleet broker. Do NOT remove the worker's existing
   `http://` peer seed.

**Go/no-go checks for Step 0 (run before observing):**

| Check | Command | Go when |
|---|---|---|
| Node attached | node: `GET /livestack/health` → `mesh.state` | reads `attached` (or the state's attached equivalent), NOT `degraded`/`absent` |
| Identity | `cap.node_id` on the same health/capability surface | reads `mesh://livestack/xc-staging-1` — daemon_id, not a key |
| Both roster rows | broker: `GET /peers` | BOTH the old `http://` row and the new `mesh://livestack/xc-staging-1/livestack` row appear (`alias_of` may link them) |
| Probe green | broker: `GET /status` | the mesh row answers `/residence` with the node's real `device_id` |
| HTTP fallback intact | broker: warm via the HTTP row (or just observe dispatch) | a warm/evict roundtrip through the HTTP peer still works |

If the mesh row sits `suspect`: read `degradation` on the row —
`mesh_tunnel_down` → relay unreachable from the node (route/firewall, check
the node's attach log); `relay_quota` → relay-side quota, not node health.
NEVER diagnose from the message text alone.

**Observation window: 7 days.** Watch, per day:
- broker `GET /peers`: the mesh row's state stays `fresh`; zero unexpected
  evictions on the node's devices (ledger: `decision=evict` rows for devices
  behind the mesh peer should be absent);
- relay restart drill (with the relay operator): restart the relay process;
  within ~`LIVESTACK_MESH_SUSPECT_PROBE_S` the row recovers to `fresh` with
  **zero** evictions (this is `test_mixed_roster.py`'s drill in production);
- one cap-key rotation (relay + broker operators together, business hours):
  present a ring with `k2` active; in-flight work must not drop; the row's
  identity must not change.

**Go to Step 1 when:** 7 days with the mesh row ≥ 99% fresh, zero mesh-caused
evictions, the relay-restart drill recovered in seconds, and the rotation was
invisible to workloads. **No-go → fix forward or `LIVESTACK_MESH_ENABLED=0`.**

## Step 1 — production flips, one host at a time

Order: pick the LEAST critical GPU host first; each subsequent host is flipped
only after the previous has been mesh-attached for ≥ 7 clean days. Between
flips, the previous host keeps both identities (mesh + HTTP) — HTTP fallback
is permanent, not a transition state.

Per host, repeat Step 0's node-side and broker-side blocks with a fresh
`LIVESTACK_MESH_DAEMON_ID` (one per host, operator-assigned, no dots). Keep
the HTTP seed row.

**Go/no-go per flip (same table as Step 0), plus:**
- `/fleet/admit` for a kind that lives on the flipped host places through the
  mesh target and the warm lands (check the node's facade log);
- one warm/evict roundtrip through the tunnel (the host's own broker
  `POST /admit` path) completes inside the 180 s warm window.

**Who flips next:** the host's own operator flips their host; the fleet
broker operator confirms the roster/probe checks before the next host starts.
The relay operator is on call for every flip (quota, cosmetics, key ring).

**Stop conditions (any → stop the sequence, revert the last host):**
- any eviction with `transport_degradation` set on a mesh peer's device;
- a mesh row stuck `suspect` > 15 min with `mesh_tunnel_down` (relay or route
  regression);
- `relay_quota` appearing under normal load (quota mis-sized for fan-out).

## Step 2 — steady state (after every host is flipped and stable)

- Leave HTTP seeds in place on LAN/VPC peers where HTTP is genuinely cheaper
  (same-rack, no NAT) — scheme selection is per-peer, and `http(s)://` peers
  remain fully supported.
- Rotate the cap key ring on the documented cadence (one TTL overlap), and the
  daemon keys per host policy — neither changes fleet identity (DR-2).
- Keep the e2e lane (`tests/test_mesh_e2e.py`) green in CI; it is the
  consolidated journey any relay/cosmetics/key change must not break.

**Ledger note (task 9.2's obligation):** record each flip in the decision
ledger as an `observe`/membership event naming the host, the daemon_id and
the flip timestamp (the membership rows already carry `transport=mesh`).

---

## Executed — 2026-09-26/27 (production rollout record)

Quota decision (operator-approved): **32 streams / 1200 req-min per account**
for the `livestack` realm (`maxStreamSeconds` inherits the global 3600).
Realm record: cosmetics `routePrefix=/livestack-relay`, `doorPath=/livestack-attach`,
`attachmentAudience=livestack-relay-attachment`, cap typ/aud `livestack-relay-*`;
HMAC ring kid `lsk1`; 15 ed25519 attachment pubkeys (one per facade identity).

**Relay doors (both serve benchday + livestack realms):**

- `zz-tower2-cn` — `https://tower2.zztech.cc:3389` — benchday relay on
  127.0.0.1:8767, livestack door process `livestack-relay` on 127.0.0.1:8768
  (nginx `/livestack-relay/` → 8768). Env: `/etc/benchday/regional-relay.env`
  (+ `/etc/benchday/livestack-relay-overrides.env`).
- `hto-histo-na` — `https://hto.zztech.io` — same topology, managed from
  zz-tower2 over ssh.

Deploy-script truth (benchday origin/main ≥ `1accb01d3`): realms-mode doors
write `BENCHDAY_RELAY_REALM` as a realm NAME; a realm file with an empty
`capabilityKeys.verify` is refused; the script installs the second door
service itself (meshlink DR-4: one engine per realm door) and points nginx at
it. Hub wiring: multi-realm `RelayTunnels` carries per-realm
`attachmentAudienceForRealm`; non-benchday door processes skip the benchday
projection/probe loop.

**Fleet state:**

| daemon_id | host | state |
|---|---|---|
| tower-asr-1 | xc-tower-ubuntu (polyasr) | attached (via CN door) |
| tower-tts | xc-tower-ubuntu (polytts) | attached (both doors) |
| tower-asr-2 | xc-tower-ubuntu (polyasr-b) | mesh env staged; engine cold-start blocked by GPU capacity (also needs `HF_HUB_OFFLINE=1`, installed as drop-in) — attaches on next successful start |
| tower-llm | xc-tower-ubuntu (harmony-llm) | mesh env staged; restart deferred — GPU1 has no room for its 0.85 fraction |
| t0-asr-1 | zz-tower0 (polyasr) | attached |
| t0-tts | zz-tower0 (polytts) | attached |
| t0-chipgen | zz-tower0 | staged; chipgen service has no mesh-capable node-py path yet |
| mac-asr-1 / mac-tts | xc-mac-studio | daemon keys staged on host; mac facades are standalone checkouts without the mesh attach path — needs the livestack node facade before flipping |
| (spares) tower-perc-1..6 | — | keys registered in the realm; reserved for future perception facades |

Brokers: `livestack-fleetd` (tower) and `livestack-fleetd-cn` (tower0) both
carry the relay env (`LIVESTACK_RELAY_URLS/IDS/CAP_KEYS`, CN broker prefers the
CN door). Tower broker roster shows `mesh://livestack/tower-asr-1/livestack`
registered.

Node drop-in shape (all flipped hosts):
`LIVESTACK_MESH_ENABLED=1`, `LIVESTACK_MESH_DAEMON_ID=<id>`,
`LIVESTACK_MESH_DAEMON_KEY_FILE` + `LIVESTACK_RELAY_KEY_FILE` (same ed25519
key — bdrt1 is minted with it and verified against its registered pubkey),
`LIVESTACK_RELAY_URLS`, `LIVESTACK_RELAY_IDS` (single-quoted JSON —
systemd strips inner quotes otherwise), `LIVESTACK_RELAY_REALM=livestack`.
The port (`mesh_outbound_py` + `websockets` + `cryptography`) is linked into
each node venv via a `mesh_outbound_py-dev.pth`. `/etc/livestack/mesh/` is
root:<svc-user> 0750, keys 0600 service-user owned; `cap-keys.json` stays
root-only.

Notable incidents during the rollout (all fixed, see benchday commits up to
`1accb01d3`): fingerprint-vs-name door selector crash-loop; empty
`capabilityKeys.verify`; `Environment=` vs `EnvironmentFile=` precedence;
nginx sed flipping both locations; stale `dist/` silently rebundled; a stray
`python3 -m http.server 8766` on tower0 squatting the polyasr port.
