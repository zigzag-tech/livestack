# Tasks — meshlink transport backbone

Every task names its tests and its ledger obligation. Phases map 1:1 to
`_plans/meshlink-backbone-plan.md`; meshlink-repo work happens in the meshlink
change `python-connectivity-consumer` and lands there first.

## Phase 0 — records (done when this change was proposed)

- [x] 0.1 DR-1..DR-5 recorded in `_plans/meshlink-backbone-decisions.md`.
      Tests: n/a. Ledger: n/a (pre-implementation decision record).

## Phase 1–3 — meshlink-side prerequisites (other repo)

- [ ] 1.1 `mesh-route-py` pyo3 crate with corpus parity. Tests: meshlink
      `cargo test -p mesh-route-core -p mesh-route-py`. Ledger: n/a.
- [ ] 2.1 `mesh_outbound_py` package, transcript replay byte-exact. Tests:
      meshlink `mesh_outbound_py` lane + relay negative cases. Ledger: n/a.
- [ ] 3.1 Relay cosmetics realm-configurable, `mesh_relay` suite green with
      zero test edits. Tests: meshlink `mesh_relay` suite. Ledger: n/a.

## Phase 4 — transport dial seam

- [x] 4.1 `livestack_node/transport.py` with `dial()`; urllib behavior moved,
      not rewritten. Tests: new `tests/test_transport.py` (record/replay
      fake). Ledger: n/a (mechanism, no decisions).
- [x] 4.2 Re-point every dial site in the seam survey; provider cloud APIs
      exempt. Tests: `cd node-py && python -m pytest -q` green. Ledger: n/a.

## Phase 5 — MeshPeer + scheme-aware identity

- [ ] 5.1 `MeshPeer` over the meshlink stack (`ls-h1` envelope, one stream
      per request). Tests: `tests/test_mesh_peer.py` against the real relay
      package (no hand-rolled mux fake — rule 11). Ledger: n/a.
- [ ] 5.2 Scheme selection in `make_peer` / `build_broker`; scheme-aware
      suffix strips. Tests: mixed-roster broker test (one HTTP peer + one
      mesh peer) in `tests/test_mixed_roster.py`. Ledger: membership records
      join with `transport=mesh` metadata (design.md).

## Phase 6 — announce path

- [ ] 6.1 Loopback self-probe documented; scheme-aware advertised URLs;
      `node_id` = daemon_id for mesh nodes. Tests: `tests/test_announce_mesh.py`
      (boot with inbound blocked). Ledger: announce/join records name the
      mesh target.
- [ ] 6.2 Node attach loop with renewal timer; failed attach reports
      unhealthy, never boot-blocks. Tests: attach-failure test asserting
      health surface. Ledger: n/a.

## Phase 7 — relay control plane

- [x] 7.1 `relay_control.py`: mint `bdrt1`/`bdsr1`, TTL clamp, renewal,
      rotation via mint/verify overlap. Tests: rotation drill — in-flight
      tunnels survive on the verify window; new attachments use the new key.
      Ledger: rotation events recorded (key id, not material).
      DONE (minting half, 2026-09-26): mint/verify for both token kinds —
      verified against the real mesh_relay package over node — TTL clamp,
      `seconds_until_refresh`, cap key ring with rotate/retire overlap, env
      config with 0600 key-file discipline, DR-3 quota declaration.
      OPEN: the rotation DRILL itself needs the mesh outbound package
      (in-flight tunnels surviving a live rotation); the ring API is shaped
      for it.
- [x] 7.2 `MESHLINK.lock` + drift check script. Tests: check script fails on
      fabricated drift. Ledger: n/a.

## Phase 8 — liveness

- [ ] 8.1 Tunnel-down → `suspect`, not `mia`; relay restart never evicts
      placements. Tests: relay-restart drill asserting placements survive.
      Ledger: every eviction on a mesh peer must cite a non-transport cause.
- [ ] 8.2 Warm timeout / in-flight TTL reconciliation over tunnels. Tests:
      dropped tunnel surfaces as failed warm. Ledger: n/a.

## Phase 9 — e2e + rollout

- [ ] 9.1 Isolated no-inbound e2e lane: attach → register → `/residence`
      probe → `/fleet/admit` → warm/evict roundtrip → relay restart → key
      rotation → quota 429. Tests: the lane itself. Ledger: the lane asserts
      ledger joins for admit/evict.
- [ ] 9.2 Staged rollout plan executed (staging host, mixed roster, then
      one-at-a-time production flips). Tests: staging observation. Ledger:
      each flip recorded.
- [ ] 9.3 Docs: HARMONY.md "Connectivity backbone" section; meshlink README
      Python-consumer row; benchday re-pin `packages/MESHLINK.lock` only.
      Tests: benchday lock check. Ledger: n/a.
