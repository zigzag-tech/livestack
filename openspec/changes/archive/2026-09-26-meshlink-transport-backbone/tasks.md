# Tasks — meshlink transport backbone

Every task names its tests and its ledger obligation. Phases map 1:1 to
`_plans/meshlink-backbone-plan.md`; meshlink-repo work happens in the meshlink
change `python-connectivity-consumer` and lands there first.

## Phase 0 — records (done when this change was proposed)

- [x] 0.1 DR-1..DR-5 recorded in `_plans/meshlink-backbone-decisions.md`.
      Tests: n/a. Ledger: n/a (pre-implementation decision record).

## Phase 1–3 — meshlink-side prerequisites (other repo)

- [x] 1.1 `mesh-route-py` pyo3 crate with corpus parity. Tests: meshlink
      `cargo test -p mesh-route-core -p mesh-route-py`. Ledger: n/a.
      DONE on meshlink (landed Phase 1, on origin/main ≤ 902a233).
- [x] 2.1 `mesh_outbound_py` package, transcript replay byte-exact. Tests:
      meshlink `mesh_outbound_py` lane + relay negative cases. Ledger: n/a.
      DONE on meshlink (landed Phase 2, ≤ 902a233).
- [x] 3.1 Relay cosmetics realm-configurable, `mesh_relay` suite green with
      zero test edits. Tests: meshlink `mesh_relay` suite. Ledger: n/a.
      DONE on meshlink (902a233 — the DR-4 realm-door e2e).

## Phase 4 — transport dial seam

- [x] 4.1 `livestack_node/transport.py` with `dial()`; urllib behavior moved,
      not rewritten. Tests: new `tests/test_transport.py` (record/replay
      fake). Ledger: n/a (mechanism, no decisions).
- [x] 4.2 Re-point every dial site in the seam survey; provider cloud APIs
      exempt. Tests: `cd node-py && python -m pytest -q` green. Ledger: n/a.

## Phase 5 — MeshPeer + scheme-aware identity

- [x] 5.1 `MeshPeer` over the meshlink stack (`ls-h1` envelope, one stream
      per request). Tests: `tests/test_mesh_peer.py` against the real relay
      package (no hand-rolled mux fake — rule 11). Ledger: n/a.
      DONE (2026-09-26): `mesh_peer.py` — MeshPeer subclasses RestPeer and
      re-points only the `_http` dial at the tunnel (both halves speak the
      ls-h1 envelope; the relay caller door is a raw byte pipe, the mux lives
      target-side). Relay candidates ranked by the mesh-route-py Picker;
      `mesh_tunnel_down` / `relay_quota` named degradations. 7 tests against
      the real `createRelayServer` + real `mesh_outbound_py` attachment,
      including relay-restart and key-rotation (DR-2) drills.
      NOTE (2026-09-26, meshlink 902a233): the two Phase-5 relay workarounds
      this task carried (forcing the benchday route prefix; minting door caps
      with benchday typ/aud) are REMOVED — the relay now strips the realm
      prefix at the WS upgrade and verifies realm cosmetics, so MeshPeer
      requests doors under `/livestack-relay` with livestack claims; see the
      DR-4 cosmetics test in test_mesh_peer.py.
- [x] 5.2 Scheme selection in `make_peer` / `build_broker`; scheme-aware
      suffix strips. Tests: mixed-roster broker test (one HTTP peer + one
      mesh peer) in `tests/test_mixed_roster.py`. Ledger: membership records
      join with `transport=mesh` metadata (design.md).
      DONE (2026-09-26): `hostd.make_peer` (http→RestPeer, mesh→MeshPeer,
      else refused by name) is the single scheme-dispatch point, used by both
      `build_broker` and the `/peers` register path; `transport.dial` refuses
      mesh:// at the seam by name (mesh dials flow through MeshPeer, not the
      urllib seam). The three `/livestack` strips share one scheme-aware
      `facade_id()`. Membership rows carry `transport=mesh` for mesh peers.

## Phase 6 — announce path

- [x] 6.1 Loopback self-probe documented; scheme-aware advertised URLs;
      `node_id` = daemon_id for mesh nodes. Tests: `tests/test_announce_mesh.py`
      (boot with inbound blocked). Ledger: announce/join records name the
      mesh target.
- [x] 6.2 Node attach loop with renewal timer; failed attach reports
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
      DONE (drill half, 2026-09-26): `tests/test_relay_rotation.py` drives a
      real tunnel through the relay harness — a slow request settles across a
      live k1→k2 rotation, a pre-minted k1 cap keeps opening the WS door on
      the verify window, k2 mints verify, and the door refuses k1 only after
      TTL + the relay's 30 s grace, after which the ring retires k1 (k2
      unaffected). The bdrt1 identity half (daemon key rotation keeps
      node_id, DR-2) is `test_mesh_peer.py::test_key_rotation_keeps_broker_
      identity_stable`.
- [x] 7.2 `MESHLINK.lock` + drift check script. Tests: check script fails on
      fabricated drift. Ledger: n/a.

## Phase 8 — liveness

- [x] 8.1 Tunnel-down → `suspect`, not `mia`; relay restart never evicts
      placements. Tests: relay-restart drill asserting placements survive.
      Ledger: every eviction on a mesh peer must cite a non-transport cause.
      DONE (2026-09-26): a probe failure carrying a NAMED degradation
      (`MeshTunnelDown`/`RelayQuotaExceeded` — only MeshPeer's dial raises
      these; urllib-era RestPeer failures never carry one, so the http path
      is provably unchanged) demotes the peer to `suspect` ON THE EVENT via
      `PeerRoster.mark_degraded` (membership.py): fresh→suspect at age 0 with
      a fast re-probe cadence (`mesh_suspect_probe_s`, default 10 s, env
      `LIVESTACK_MESH_SUSPECT_PROBE_S`), placements kept by the existing
      `_remembered_peer` rule until `mia` at 600 s — age still owns mia, and
      the override never applies there, so a long-dead seed is not
      re-dialled every few seconds forever. One recovery (a successful
      snapshot) clears the demotion back to fresh. Drills:
      `test_mixed_roster.py::test_relay_restart_drill_placements_survive_
      suspect_zero_evictions` extends Phase 5's naming test at the broker
      level (no duplication): suspect at age 0 on tunnel loss, remembered
      placement feeds the world across the episode, fast re-probe re-attaches
      after `relay_up` with zero evictions on either transport, and the
      observe records carry `request.membership{degradation,
      suspect_after_s, mia_after_s, probe_every_s}` — every knob joined by
      decision_id. Evict ledger records gain `request.transport_degradation`
      when the target device belongs to a transport-degraded peer, so an
      eviction on a mesh peer cites the planner's non-transport cause beside
      the transport state. State-machine units: 4 new tests in
      `test_membership.py`.
- [x] 8.2 Warm timeout / in-flight TTL reconciliation over tunnels. Tests:
      dropped tunnel surfaces as failed warm. Ledger: n/a.
      DONE (2026-09-26): the warm path needed one reconciliation, now proven
      by drill rather than assumed. (a) RestPeer.warm's 180 s semantics ARE
      preserved over tunnels — MeshPeer._dial budgets every await against the
      caller's timeout, so a stalled tunnel surfaces well inside the window
      (proof: `test_warm_over_stalled_tunnel_fails_within_warm_window_not_
      stuck`, ceiling-asserted at 60 s, actual ~ms). (b) The 900 s in-flight
      record was NOT reconciled: `plan_and_apply` kept it even when the dial
      died before the request was sent, reserving the card against a load
      that never started. `MeshPeerError` now carries `dispatched` (False
      for door-refused/429/connect-timeout/no-candidate failures, True for
      mid-response deaths where the load may genuinely be running); the
      broker drops the in-flight record and logs "failed before dispatch"
      when `dispatched is False`, keeping the bounded-TTL behavior for the
      ambiguous mid-response case. Drill asserts the in-flight record is
      gone, the warm never reached the facade, and a re-warm over the
      recovered tunnel succeeds.

## Phase 9 — e2e + rollout

- [x] 9.1 Isolated no-inbound e2e lane: attach → register → `/residence`
      probe → `/fleet/admit` → warm/evict roundtrip → relay restart → key
      rotation → quota 429. Tests: the lane itself. Ledger: the lane asserts
      ledger joins for admit/evict.
      DONE (2026-09-26): `tests/test_mesh_e2e.py` — the consolidated
      single-journey lane. No-inbound is SIMULATED (a real netns is not
      available unprivileged in CI — `unshare -n` fails EPERM — so every
      listener is loopback-only and the lane asserts the node's env-resolved
      config names no dialable non-loopback address; stated in the lane's
      docstring and commit). Register runs through the real `register_url`
      path (what `POST /peers` serves) with the MeshPeer built by the real
      `make_peer` from the caller relay env; `/fleet/admit` runs through the
      real `build_app` route over ASGI. Rotation rides the verify window
      (expiry stays in test_relay_rotation.py, daemon-key half in
      test_mesh_peer.py — DRY). Ledger joins asserted: admit record ↔ hosted
      lease (lease_id both directions; decision_id when a policy runtime
      records one) and grant/load/evict records on the journey owner, with
      every eviction citing a non-transport cause.
- [x] 9.2 Staged rollout plan executed (staging host, mixed roster, then
      one-at-a-time production flips). Tests: staging observation. Ledger:
      each flip recorded.
      RUNBOOK DELIVERED (2026-09-26): `_plans/meshlink-rollout.md` — staging
      step with concrete env/commands and go/no-go checks, per-host
      production flips with stop conditions and named operators. The
      observation windows and flips themselves are OPERATOR TIME and have
      NOT been executed; the runbook says so.
- [x] 9.3 Docs: HARMONY.md "Connectivity backbone" section; meshlink README
      Python-consumer row; benchday re-pin `packages/MESHLINK.lock` only.
      Tests: benchday lock check. Ledger: n/a.
      DONE (2026-09-26): HARMONY.md gained the section + the mesh env rows
      (incl. `LIVESTACK_MESH_SUSPECT_PROBE_S` and the health-surface
      semantics); the meshlink README Python rows were already there from
      Phase 2 — the missing realm-door e2e line was added (meshlink
      c6ab96f); benchday re-pinned d77e1e24 → c6ab96f (includes DR-4's
      902a233) and `check-submodule-pins.mjs --meshlink-only` is green on
      the landed tree (benchday origin/main 021c8273d).
