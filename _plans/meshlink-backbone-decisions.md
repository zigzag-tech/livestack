# meshlink backbone — decision record (DR-1..DR-5)

**Status:** DECIDED (2026-09-26), pending implementation via
`_plans/meshlink-backbone-plan.md` (10 phases).
**Realises:** the "one implementation" ruling recorded in
`benchday/docs/mesh-route.md` (2026-09-21) — Livestack binds meshlink, never
forks it; conformance is proven against sha256-pinned fixtures.
**Supersedes:** nothing. Livestack today assumes all daemons/coordinators live
on one LAN/VPN; every inter-node dial is plain HTTP/urllib to a reachable IP.

Each decision below names what was decided, why, what it rules out, and the
phase that implements it. A second opinion (Claude Code CLI, Sonnet,
2026-09-26) reviewed the plan; its accepted findings are folded into the
relevant DRs and its one rejection (multi-broker fanout is fine — the relay mux
is multi-session, cap 32) is recorded here so the worry is not re-raised.

## DR-1 — Control plane: Livestack runs its own realm on the shared relay

**Decision.** Livestack operates in its own relay realm (`livestack`) with its
own ed25519 attachment key (PKCS#8, file mode 0600, same discipline as
`LIVESTACK_FLEET_TOKENS_FILE`) and its own HMAC key ring for `bdsr1` caller
capabilities. One relay process serves both realms; benchday's hub realm is
untouched. Token minting in Python: stdlib `hmac`/`hashlib` for `bdsr1` caps,
`cryptography` for `bdrt1` attachment tokens.

**Why.** Realm isolation is cryptographic, not cosmetic
(`meshlink/packages/mesh_relay/src/realms.ts:10-11` anticipated a second
consumer). A Livestack capability or attachment token is invalid in benchday's
realm and vice versa, so a bug or compromise in one fleet cannot pivot into
the other. Running a second relay process was rejected: it doubles the
operational surface (door TLS, quota config, restart drills) for zero
isolation gain.

**Rules out:** sharing benchday's hub realm; per-fleet relay processes; minting
tokens against benchday's key ring.

**Implements:** plan Phase 7 (`relay_control.py`); meshlink Phase 3 (relay
generalization).

## DR-2 — Identity: stable identity is `realm + daemon_id`, not the key

**Decision.** A mesh-attached node's stable identity is `realm + daemon_id`
(operator-assigned, e.g. `gpu-box-7`), NOT its ed25519 key. Key rotation must
not mint a new peer. Peer URL form: `mesh://livestack/<daemon_id>/livestack`.

**Why.** The relay authenticates attachments by ed25519 signature, but the
*fleet* identifies nodes by operator-assigned name (placements, ledger joins
and roster records key on it). If the key were the identity, every rotation
would orphan placements and ledger history — an operational impossibility, so
rotation would simply never happen, which is a worse security posture.
(Second-opinion finding, accepted.) The daemon_id is carried in the attach
token (`bdrt1` daemon field) and in the peer URL path, so the mapping
key→daemon_id is validated at attach time, not trusted from a roster claim.

**Rules out:** key-derived node ids; treating a rotated key as a new node;
operator-free daemon_id assignment (operator assigns, daemon persists it).

**Implements:** plan Phase 5 (MeshPeer, `_node_id_seen` stability),
Phase 6 (node_id uses daemon_id).

## DR-3 — Quota: explicit livestack-realm quota at deploy time

**Decision.** The relay default (4 concurrent streams / 240 req-min per
realm+account) is too low for broker fan-out. Livestack's realm quota is set
explicitly at relay deploy time (initial values chosen when the relay host is
provisioned, recorded in this fleet's deploy doc); Livestack's own
`max_concurrent_per_account` policy remains and the two limits are documented
as stacked, not merged.

**Why.** A broker warming N nodes opens N streams; with a relay cap of 4 the
fan-out queues against itself while the fleet policy would have allowed more.
But raising the relay cap globally would weaken benchday's realm, so the quota
is per-realm config, not a global change. Stacking is honest: either limit may
bind first, and a 429 from the relay must surface as a named degradation
("relay quota") not a generic failure (jidoka — absence and failure must not
look alike).

**Rules out:** changing the relay default; treating relay 429s as node
unhealth.

**Implements:** plan Phase 7 (config surface) and Phase 9 (quota-refusal
assertion in the e2e).

## DR-4 — Relay cosmetics: realm-configurable prefix and token audience

**Decision.** The relay's path prefix (`/benchday-relay`) and token `aud`/`typ`
strings become realm-configurable, with defaults byte-identical to today's
behavior. Benchday's realm runs with defaults; the livestack realm may use its
own prefix (e.g. `/livestack-relay`).

**Why.** A livestack node attaching to a door whose every route says
"benchday" is a lie an operator will trip over exactly once, at 2 a.m.
Cosmetics must not fork the protocol, so this is config, not code paths —
and the config defaults are pinned by the existing `mesh_relay` test suite
passing with zero edits.

**Rules out:** forking `mesh_relay` into a livestack flavor; renaming
benchday's prefix.

**Implements:** meshlink plan Phase 3.

## DR-5 — Pinning: one lock covering crate and relay

**Decision.** Livestack keeps a `MESHLINK.lock` (meshlink commit rev)
covering **both** the pyo3 route crate and the relay version it is tested
against. A check script fails CI on drift, adapted from benchday's
`scripts/check-submodule-pins.mjs` discipline. Benchday independently
re-pins `packages/MESHLINK.lock` when it takes new meshlink versions; the two
pins may differ and each repo owns its own.

**Why.** The conformance guarantee ("binds, never forks") is only as good as
the pin: a drifting consumer may be running a relay whose mux bytes the
checked-out crate never saw. One lock for both halves because they ship
together from one meshlink commit; two repos each owning their own pin because
neither repo may be blocked on the other's cadence.

**Rules out:** version-range dependencies on meshlink; benchday's pin
governing livestack (or vice versa).

**Implements:** plan Phase 7 (lock + check script), Phase 9 item 4 (benchday
re-pin only).

## Resolved second-opinion findings (for the record)

- **Circular self-probe (accepted):** `facade_answers()` in `announce.py`
  probes loopback (the tunnel's termination) while `register_once` announces
  the `mesh://` target — two addresses, one door. Documented in the
  docstring; fixed semantics in plan Phase 6.
- **Key-rotation identity stability (accepted):** folded into DR-2.
- **Membership flapping → spurious evictions (accepted):** a relay restart
  must demote mesh peers to `suspect`, never straight to `mia`/evict. Plan
  Phase 8.
- **Quota stacking (accepted):** folded into DR-3.
- **Dual lockfile pinning (accepted):** folded into DR-5.
- **Multi-broker fanout worry (rejected):** the relay mux is multi-session
  (cap 32); one node serving several brokers is a supported shape, not a
  hazard.
