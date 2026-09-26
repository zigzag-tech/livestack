# Livestack ↔ meshlink integration — full refactor plan

**End goal:** Livestack brokers/nodes/coordinators communicate across the mesh with
meshlink as the connectivity backbone: outbound-only WSS attach to relay doors,
ed25519/HMAC token auth, route-policy-driven endpoint selection (Rust core), and
zero requirement for inbound ports on fleet nodes. Plain-HTTP-over-mesh peers keep
working side by side during and after migration (a peer is an opaque URL string in
both rosters today — that is what makes this incremental).

**Repos:** `/home/ubuntu/meshlink` (connectivity kernel), `/home/ubuntu/livestack`
(Harmony fleet). Benchday is *not* modified; it only re-pins `packages/MESHLINK.lock`
when taking new meshlink versions.

**Hard constraints (carried from the 2026-09-21 "one implementation" ruling in
`benchday/docs/mesh-route.md`):** Livestack binds, never forks. Conformance is proven
against sha256-pinned fixtures, not recollections. Livestack's realm is cryptographically
isolated from benchday's (`meshlink/packages/mesh_relay/src/realms.ts:10-11` anticipated
exactly this). No listener changes on fleet nodes — tunnels terminate on loopback HTTP.

---

## Phase 0 — Decision record + OpenSpec changes (both repos, no code)

Write `livestack/_plans/meshlink-backbone-decisions.md` resolving, in writing, before
any transport code:

- **DR-1 Control plane:** Livestack runs its own control plane in its own relay realm
  (`livestack`), with its own ed25519 attachment key (PKCS#8, mode 0600) and its own
  HMAC key ring for `bdsr1` caller capabilities. One relay process serves both realms;
  benchday's hub realm is untouched. Minting code: stdlib `hmac`/`hashlib` for caps,
  `cryptography` for `bdrt1`.
- **DR-2 Identity:** a mesh-attached node's stable identity is `realm + daemon_id`
  (operator-assigned, e.g. `gpu-box-7`), NOT its ed25519 key — key rotation must not
  mint a new peer. Peer URL form: `mesh://livestack/<daemon_id>/livestack`.
- **DR-3 Quota:** relay default (4 concurrent streams / 240 req-min per realm+account)
  is too low for broker fan-out; set explicit livestack-realm quota at deploy time and
  document that it stacks under Livestack's own `max_concurrent_per_account` policy.
- **DR-4 Relay cosmetics:** path prefix (`/benchday-relay`) and token `aud`/`typ`
  strings become realm-configurable, defaults unchanged (benchday byte-compatible).
- **DR-5 Pinning:** Livestack keeps a `MESHLINK.lock` (meshlink commit rev) covering
  **both** the pyo3 crate and the relay version; a check script fails CI on drift —
  benchday's `scripts/check-submodule-pins.mjs` discipline, adapted.

Then `openspec new change` on each repo — meshlink: *"python connectivity consumer"*
(new pyo3 crate + Python outbound port + relay generalization); livestack: *"meshlink
transport backbone"* (transport seam, MeshPeer, announce, control plane, liveness).
Proposals/designs/tasks + delta specs before code, per both repos' AGENTS rules.

## Phase 1 — meshlink: pyo3 route-core binding

1. New crate `rust/mesh-route-py` (pyo3 + serde_json; build via maturin, matching
   livestack `shared-py/` conventions). Mirror the WASM surface exactly
   (`rust/mesh-route-wasm/src/route_wasm.rs:38-119`): `Picker` class with
   set_candidates / ranked / pick_best / snapshot / the seven `record_*` hooks /
   reset_quarantines; JSON in/out; `contract_version()` guard at import — a
   mismatch is an explicit error, never a silent fallback (jidoka).
2. Contract tests: same route-plan JSON corpora used by Dart and Rust
   (`packages/mesh_transport/fixtures/route_plan/ladder.json`,
   `direct_cooldown.json`, sha256-pinned) run through the pyo3 binding; outputs
   must equal the wasm binding's.
3. Land via `scripts/land.sh`; `tool/check_boundary.dart` stays green; core untouched.

**Gate:** `cargo test -p mesh-route-core -p mesh-route-py`; corpus parity green.

## Phase 2 — meshlink: Python outbound (attach-side) port

1. New package `packages/mesh_outbound_py` (pure Python; dep: `websockets`). Port
   `packages/mesh_outbound/src/index.ts` (~150 lines of logic): `bdrt1` attach token
   presentation, ed25519 challenge/response (5 s deadline), `[type u8][id u32be]
   [payload]` mux, `OPEN/DATA/CLOSE/PING/PONG`, text-frame token renewal (realm /
   daemon / key / account immutable), dial backoff 1 s→60 s reset only after a
   `ready` that served ≥30 s, ≤2 attachments per relay URL.
2. **Pluggable local connector** (the design extension livestack needs): the TS
   original bridges each stream to a local WebSocket (`local.wsUrl`); the Python
   port additionally ships an **HTTP connector** — each `OPEN` maps to one loopback
   HTTP request via `urllib`, response framed back over `DATA`/`CLOSE`. Mux bytes on
   the wire are identical; only the termination changes. Livestack uses HTTP mode.
3. Conformance: replay `packages/mesh_relay/fixtures/tunnel/transcript.bin`
   (sha256-pinned, recorded from benchday's Rust daemon) — byte-exact; plus the
   negative cases from `mesh_relay` tests (`invalid_auth`, ping-timeout, 4 MiB cap).
4. Update meshlink `README.md` package table + `AGENTS.md` boundary rules for the
   new Python packages.

**Gate:** transcript replay byte-identical; new lane green in meshlink CI.

## Phase 3 — meshlink: relay generalization (config-only)

1. Path prefix and token `aud`/`typ` become realm config (DR-4); defaults unchanged.
2. Document env-seeded targets (`MapOptions.seeds` floor in `target_map.ts`) as the
   livestack-relay bootstrap; a dynamic projection feed is optional follow-up.
3. Prove default behavior byte-compatible: existing `mesh_relay` tests unchanged and
   green.

**Gate:** `mesh_relay` suite green with zero test edits.

## Phase 4 — livestack: transport dial seam

1. New `node-py/livestack_node/transport.py`: `dial(target, method, path, headers,
   body, timeout) -> (status, headers, bytes)`. Default impl is the current urllib
   behavior, moved, not rewritten.
2. Re-point every dial site found in the seam survey: `announce.py:173,217`;
   `hostbroker.py:1249` (measure_links), `_http` at 1477-1490 and its call sites
   (1552, 1585, 1680, 1688, 1692); `client.py:41,54,66,74,147,196`;
   `perception/serve.py:30,113`, `perception/remote.py:52,60`;
   `workloads/client.py:24, transfer.py:41, download.py:29, lease_helper.py:26`;
   `policy_lab/profile_worker.py:21,51`. Provider cloud APIs (Aliyun/RunPod) are
   exempt — not fleet connectivity.
3. Listener sites (`hostd.py:1410`, uvicorn embeds, `workloads/http.py:73`) untouched.

**Gate:** `cd node-py && python -m pytest -q` green; new unit tests for transport
fake (record/replay) — no hand-rolled fakes for the mesh path (rule 11).

## Phase 5 — livestack: MeshPeer + scheme-aware identity

1. `MeshPeer` implements the existing `Peer` duck type (`hostbroker.py:108-119`)
   over the meshlink stack: route policy from the `mesh-route-py` Picker (relay
   candidates from the control plane's target manifest), caller-side dial = open
   WSS to `<relay>/<prefix>/<route>/<daemon_id>?cap=<bdsr1>`, HTTP request/response
   enveloped over the tunnel (`ls-h1` envelope: method, path, headers, body — one
   stream per request, mirroring the target-side HTTP connector).
2. Scheme selection in `make_peer` (`hostd.py:341-347`) and `build_broker`
   (`hostd.py:131-144`): `http(s)://` → RestPeer; `mesh://` → MeshPeer. Planning,
   membership, pruning untouched — peers remain opaque URL-keyed records.
3. Make the three `/livestack` suffix-strip sites scheme-aware
   (`fleet_admit.py:67-68`, `hostd.py:622-624`, `hostd.py:1093-1096`).
4. Dedup: `_node_id_seen` (`hostbroker.py:654-661, 718-740`) must treat
   `mesh://livestack/<daemon_id>` as stable across key rotation and reconnects
   (DR-2).

**Gate:** pytest green with a mixed roster (one HTTP peer + one mesh peer) in the
same broker — this is the incremental-migration proof.

## Phase 6 — livestack: announce path

1. Resolve the circular self-probe Claude flagged: `facade_answers()`
   (`announce.py:156-176`) probes **loopback** (the tunnel's termination), while
   `register_once` announces the `mesh://` target. Two addresses, one door — write
   it down in the docstring.
2. `serve.py:224-256` advertised-URL construction becomes scheme-aware;
   `LIVESTACK_NODE_HOST` may name a mesh daemon_id; `node_id` (serve.py:189) uses
   the stable daemon_id for mesh-attached nodes.
3. Node side: attach loop (Phase 2 port, HTTP-connector mode) started by `attach()`
   after the local facade is serving; renewal timer at ~240 s (token exp ≤300 s);
   a failed attach must report unhealthy (jidoka — `recordSubsystemFailure`
   equivalent), never boot-block.

**Gate:** node boots with no reachable IP (test host with inbound blocked), attaches
outbound, registers `mesh://` target, broker probes it green.

## Phase 7 — livestack: relay control plane

1. New `node-py/livestack_node/relay_control.py`: mints `bdrt1` attachments and
   `bdsr1` caller caps (DR-1), TTL clamped 30 s–15 min per relay rules, renewal
   scheduling, key rotation via the ring's mint/verify overlap.
2. Config surface: `LIVESTACK_RELAY_URLS` (comma list), `LIVESTACK_RELAY_REALM`,
   `LIVESTACK_RELAY_KEY(_FILE)`, `LIVESTACK_RELAY_CAP_KEYS`, quota envs. Same
   0600-file discipline as `LIVESTACK_FLEET_TOKENS_FILE`.
3. Quota set per DR-3; the relay's per-stream cap and Livestack's
   `max_concurrent_per_account` documented as stacked limits.

**Gate:** rotation drill in tests: active key rotated, in-flight tunnels survive on
the verify window, new attachments use the new key.

## Phase 8 — liveness mitigations

1. Membership: mesh peers distinguish *tunnel down* (transient — enter `suspect`,
   keep placements, fast re-probe) from *peer dead* (`mia` at 600 s, placements
   dropped). Never let a relay restart trigger unit evictions.
2. `RestPeer.warm()` 180 s timeout semantics preserved over tunnels; reconcile the
   900 s in-flight TTL (`hostbroker.py:998-1008`) so a dropped tunnel surfaces as a
   failed warm, not a stuck in-flight.
3. Record every knob in the decision ledger (livestack already has one — join
   records by `decision_id`).

## Phase 9 — integration e2e + staged rollout

1. **Isolated e2e (livestack's own lane):** relay (Phase 3 build) + broker + one
   node in a no-inbound netns. Assertions: outbound attach → register → `/residence`
   probe → `/fleet/admit` → `warm`/`evict` roundtrip → relay restart recovery → key
   rotation → quota refusal (429). This is the fixture the rule "if the environment
   cannot express the behavior, the change adds the fixture that lets it" demands.
2. **Staging:** one real host (recommend an xc worker) attaches via mesh alongside
   its HTTP peer identity; mixed roster observed for a week.
3. **Production mesh:** flip hosts one at a time; HTTP scheme stays as permanent
   fallback for mesh-joined LAN/VPC peers (zero deprecation pressure).
4. **Docs:** HARMONY.md gains a "Connectivity backbone" section; meshlink README
   gains the Python-consumer row; benchday untouched except the lock re-pin.

---

## Effort shape

Phases 0–3 are meshlink-side (~1 week, much of it conformance plumbing). Phases 4–8
are livestack-side (~1.5–2 weeks). Phase 9 is a week of e2e + staged rollout. The
critical path is Phase 0 (the five decisions) — everything else parallelizes:
Phase 1 is fully unblocked today; Phase 4 is fully unblocked today; both can run
before Phase 0 concludes, but no mesh-path code merges before the DR lands.

## End-state architecture

```
fleet node (no inbound ports)                relay door (TS, both realms)
  uvicorn facade @ loopback  ◀─HTTP─  mesh_outbound_py (mux, HTTP connector)
       ▲ attach outbound WSS /daemon-attach (bdrt1, ed25519 challenge) ─────▶
broker / callers                                                              │
  MeshPeer ── WSS <relay>/<prefix>/<route>/<daemon_id>?cap=<bdsr1> ───────────▶
  route policy: mesh-route-py (pyo3 over rust/mesh-route-core, sans-io)
```
One policy implementation (Rust), one relay implementation (TS), one mux on the wire
(sha256-pinned), two app payloads (benchday's, livestack's HTTP-over-stream) — and
Livestack nodes reachable from anywhere with only outbound 443.
