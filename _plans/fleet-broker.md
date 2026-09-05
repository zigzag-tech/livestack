# Fleet Broker — one Harmony that can see every host

**Status:** DESIGN, ready for implementation handoff. Nothing in this document
is built. 2026-09-05.

**Companions:** `harmony-gaps-2026-09.md` is the evidence this answers.
`decision-ledger.md` is the retrospection requirement every phase here must
satisfy — read it before implementing any phase, because each phase adds an
emitter to it. Builds on `peer-membership.md` (who exists) and
`resource-planner.md` (placement). Sits beside `fleet-scheduler.md`, which
decides when to *buy* capacity; this decides where *existing* capacity is,
how busy it is, and how far away.

**The operator's goal, verbatim:** *throw a long task, a small task, or a
stream at the fleet, and the whole fleet knows how to self-organize and
allocate resources — based on topology, geography, pings, capacity, free
memory.* This document is the path from what exists to that, in phases that
each ship something usable and each leave a decision trail a future agent can
audit.

---

## 0. Ground truth — what exists, what does not

Verified against the code and the running fleet on 2026-09-05.

| capability | exists? | where |
|---|---|---|
| per-host GPU residency arbitration (load/evict/defer by priority, tier, measured VRAM) | **yes, works** | `planner.py`, `hostbroker.py`, `hostd.py` — evicted an over-budget LLM within seconds |
| federation mechanics (one broker planning over peers on many hosts) | **yes, dormant** | `HostBroker.snapshot()` discovers devices from peers' `host_id`/`device_id` — *"That is the whole of federation"* — but every `hostd` seeds three localhost URLs |
| a fleet-wide view | **no** | tower0's broker has never heard of xc-tower-ubuntu |
| observe-only mode (plan without dispatching) | **no** | `plan_and_apply` always dispatches; a fleet broker pointed at remote nodes would fight the host brokers |
| membership that certifies a node is *serving* | **no** | `membership._upsert` marks a peer seen on every announce; a wedged node announced itself `fresh` for 7 h |
| network latency / RTT / geography / region model | **no** | zero references in `livestack_node`; the only spatial notion is `locality_host == host_id`, a flat penalty of 2.0 |
| per-node load on the readiness descriptor | **yes (2026-09-05)** | `facade.py` `load` block: `in_flight`, `pressure`, `device`; polyasr supplies its own `in_flight` |
| cross-target scheduler | **yes, unused by benchday** | `fleet_scheduler.py` — cost/deadline/elastic-burst; no latency term; referenced only by `fleet_dispatch.py`, `provision.py` |
| the thing that actually routes speech today | **client-side** | benchday `packages/mesh_route` `MeshRoutePicker` (latency EWMA + load tie-break); hub only publishes a *list* |
| a hub→worker job lane | **yes** | benchday `hub/src/execution/backend.ts`: `ExecutionBackend.runJob(name, input)`, modes `local-direct` \| `livestack-gateway` |

Live topology measured from xc-tower-ubuntu (Vaughan, ON), `GET /livestack/residence`:

| node | probe |
|---|---|
| zz-tower0 (Nanjing) :8766 / :8100 / :8844 | 1089 / 1533 / 522 ms |
| xc-mac-studio (Thornhill) :8765 | 35 ms |
| xc-mac-studio :8100 (polytts) | **unreachable — binds 127.0.0.1 only** |
| xc-tower-ubuntu (local) ×4 | ~2 ms |

That table *is* the fleet's topology, and nothing in Harmony can see it.

---

## 1. Architecture

```
                      ┌──────────────────────────────────────────┐
                      │  FLEET BROKER  (hostd, LIVESTACK_DISPATCH=observe)
                      │  peers = every node facade on every host  │
                      │  snapshot → plan (never dispatched)       │
                      │  GET /fleet     the whole-fleet view       │
                      │  GET /fleet/rank?kind=asr&region=na  (P2) │
                      │  POST /fleet/admit  (P3)                   │
                      │  emits: decision-ledger records            │
                      └───────┬──────────────┬──────────────┬─────┘
             probes /residence│              │              │
                 /capability  │              │              │
        ┌─────────────────────▼──┐  ┌────────▼─────────┐  ┌─▼──────────────────┐
        │ zz-tower0              │  │ xc-mac-studio     │  │ xc-tower-ubuntu     │
        │ hostd :8799 (dispatch) │  │ hostd :8799       │  │ hostd :8799         │
        │ polyasr polytts chipgen│  │ polyasr polytts   │  │ polyasr×2 polytts llm│
        └────────────────────────┘  └───────────────────┘  └─────────────────────┘
                     ▲ each host broker keeps SOLE authority to warm/evict its own cards
```

Three rules that make this safe to add to a running fleet:

1. **One card, one master.** Only the host broker on a machine may warm or
   evict units on that machine. The fleet broker observes. Phase 5 relaxes this
   to *warm-only, via the host broker, never a node* — and never evict.
2. **Soft state everywhere.** The fleet broker holds nothing durable. Restart
   ⇒ refilled from nodes within one probe cycle. Losing it ⇒ today's behaviour
   exactly (host brokers arbitrate their cards; clients probe). It is a single
   point of *insight*, not of failure.
3. **Consumers of the view degrade to the current behaviour when the view is
   absent or stale.** The manifest still lists every granted target; the
   picker still probes; jobs still run on `local-direct`. A stale fleet
   ranking is discarded, not trusted.

---

## 2. Phase 0 — prerequisites (a fleet view built on lies is worse than none)

Each item is small, has a reproduction, and ships independently. **Do these
before Phase 1.** Order within the phase does not matter.

### 0.1 An announce registers; only a snapshot certifies

**File:** `node-py/livestack_node/membership.py`, `PeerRoster._upsert`.
**Change:** delete the `self.mark_seen(key)` call at the end of `_upsert`. A
*new* record still gets `last_seen=now` at creation (one grace window of
`suspect_after_s` to be snapshotted). A *renewal* advances nothing. The only
path to `fresh` after that is `HostBroker.snapshot()` succeeding on the
facade (which already calls `roster.mark_seen`). Seeds are unaffected; they
were never certified by announce.
**Doc:** update the one sentence in `peer-membership.md` — "membership is a
report, not a promise" stays for *absence* (a blip does not drop a peer);
add "presence needs proof."
**Tests:** `tests/test_membership.py` — a registered peer that is never
snapshotted goes `fresh → suspect → mia` on the normal clock; a renewal does
not reset the age; a successful `mark_seen` still restores `fresh` at once.
Existing `test_any_single_success_returns_a_peer_to_fresh_immediately` must
still pass unchanged.
**Repro it fixes:** xc-mac-studio polyasr, 2026-09-05 — 7 h dead, listed
fresh.

### 0.2 The registrar announces only a facade that answers

**File:** `node-py/livestack_node/announce.py`.
**Change:** add `facade_answers(facade_url) -> bool` (GET
`{facade_url}/residence`, 2 s timeout, 200 ⇒ True). In `start_registrar`'s
loop, before `register_once`, if `not facade_answers(...)`: wait `backoff`
(doubling to `RETRY_MAX_S`) and `continue`. Reset backoff on success.
**Tests:** `tests/test_announce.py` (new) — with a fake facade that 503s, no
registration is sent; once it 200s, exactly one "reported for duty" log.
**Why:** `attach()` starts the thread at import, before bind. A server that
never binds must not claim duty.

### 0.3 Device identity comes from the node, not from a string template

**Files:** `facade.py` (`device_id` computed in `build_router`), `serve.py`
(`attach()` signature), `hostbroker.py` (`RestPeer.device_id`).
**Today:** `device_id = f"{capability.host_id}/gpu0"` — hardcoded suffix.
**Change:** `attach(..., device_id: Optional[str] = None)`. Resolution order:
explicit argument → `LIVESTACK_DEVICE_ID` env → derived: on CUDA,
`f"{host_id}/{torch.cuda.get_device_properties(current_device()).uuid}"`
(short-hashed to 8 hex); on MLX, `f"{host_id}/mlx0"`; else `f"{host_id}/gpu0"`
(today's value — no behaviour change for single-GPU nodes that pass nothing).
**Also:** `HostBroker.snapshot()` keys `units` by `kind` → collides when two
nodes on one device host the same kind. Key the internal `units` map by
`(kind, peer_key)` and expose per-kind aggregates to the planner as it expects
(the planner's `Unit` is per-kind; two polyasr on one card are two
*placements* of one unit, which `placements` already models).
**Tests:** two fake peers reporting the same `device_id` and kind produce one
device with two placements, not two devices; a node passing an explicit
`device_id` sees it on `/capability` and `/residence`.
**Repro it fixes:** a second polyasr on xc-tower-ubuntu needed a fake
`host_id` to get its own device; a stale one later made the planner co-model a
5 GB engine and a 20 GB LLM on one card and evict the LLM.

### 0.4 `in_flight` is a contract, not a workaround

**Files:** `serve.py`, `facade.py`.
**Change:** `attach(..., in_flight: Optional[Callable[[], int]] = None)`. When
supplied, `_load_report` uses it for `load.in_flight`; when absent, the
lease-derived count is used **and** the report carries
`"in_flight_source": "leases"` so a consumer can tell "0 because idle" from
"0 because this node cannot see its own work." Provide a helper context
manager `livestack_node.counting()` that servers wrap handlers in.
**Migrate:** polyasr `cuda/server.py` and the MLX `server.py` (both currently
supply it via `readiness()`; move to the new argument), polytts, harmony-llm.
**Tests:** extend `tests/test_capability_load.py`.

### 0.5 The meter must agree with the node's device

**File:** `meters.py`, `serve.py`.
**Change:** `auto_meter()` takes the resolved `device_id` (0.3) and, on CUDA,
verifies `current_device()` maps to it; on mismatch it returns a meter that
reports **nothing** and logs once: `"[livestack] meter refused: process is on
{a}, meter would read {b}"`. `pressure` then falls to *no opinion* rather than
a confident wrong number.
**Repro:** three separate bugs on 2026-09-05, one root — two nodes on
different cards reporting byte-identical pressure.

### 0.6 Route-core parity (doctrine violation, not a gap)

**Files:** `shared/src/route.rs`, `shared-wasm/src/route_wasm.rs`.
**Change:** port the three 2026-09-05 changes from benchday
`packages/mesh_route/lib/mesh_route.dart` to `route.rs`, with the
`load_distribution_test.dart` cases ported as Rust tests:
(a) `load_aware` + `record_load(key, pressure, in_flight)`, queue depth
primary, pressure may only raise the estimate, applied only inside
`explore_band`, both sides must report or load decides nothing;
(b) `remeasure_after` with per-candidate backoff — the stalest overdue
eligible candidate is promoted to the front; never-picked candidates exempt;
(c) a sample recorded after a forced re-measure **replaces** the EWMA.
**Why first:** `route-selection.md` — *"Any behavioural change must land in
both or in neither."* Currently it is in one.

---

## 3. Phase 1 — the fleet broker, observe-only

### 3.1 Code

**`hostbroker.py`**

- `HostBroker.__init__(..., dispatch: bool = True)`; store `self.dispatch`.
- `self.probe_ms: Dict[str, float]` — per peer, EWMA (0.7 old / 0.3 new) of
  the wall-clock cost of `p.units()` + `p.placements()` in `snapshot()`. This
  is the first, cheapest RTT: already paid for by the probe that has to
  happen. Measured *from where the broker sits*.
- `plan_and_apply()`: after `p = plan(...)`, `if not self.dispatch: return p`.
- `membership_snapshot()`: add `probe_ms` per row when known.
- `RestPeer.capability()`: `GET {base}/capability`, uncached.
- `fleet_view() -> dict` (shape in §3.3).

**`hostd.py`**

- `LIVESTACK_DISPATCH` env: `apply` (default) \| `observe`. Pass through
  `build_broker(..., dispatch=)`.
- `GET /fleet` → `broker.fleet_view()`.
- Startup line names the role: `fleet broker (observe-only)`.
- Docstring: document the new env and endpoint.

Nothing else changes. A host broker with defaults behaves byte-for-byte as
before.

### 3.2 Deployment

| | |
|---|---|
| unit | `livestack-fleetd.service` on **xc-tower-ubuntu** |
| why there | outside the GFW; reaches every node directly; the alternative (zz-tower2) would measure every NA node through DERP and every probe would carry that penalty |
| port | 8801 (8799 is the local host broker; 8800 is buildd on tower2) |
| venv | `/home/ubuntu/polyasr/cuda/venv` (has `livestack_node` + `shared_py`), same arrangement as the host broker |
| logs | journald; bounded by `/etc/systemd/journald.conf.d/10-benchday-gpu-nodes.conf` (4 GiB / 30 d) — rule 10 |
| env | see below |

```
LIVESTACK_DISPATCH=observe
LIVESTACK_BROKER_PORT=8801
LIVESTACK_REPLAN_INTERVAL=15
LIVESTACK_PEER_SUSPECT_SECONDS=60
LIVESTACK_PEER_MIA_SECONDS=900
LIVESTACK_PEERS=http://100.64.0.3:8766/livestack,http://100.64.0.3:8100/livestack,http://100.64.0.3:8844/livestack,http://100.64.0.2:8765/livestack,http://100.64.0.2:8100/livestack,http://100.64.0.18:8766/livestack,http://100.64.0.18:8767/livestack,http://100.64.0.18:8100/livestack,http://100.64.0.18:8188/livestack
```

Seeds, not announces, on purpose: nodes announce to one
`LIVESTACK_BROKER_URL` today, and an operator writing a node down is the
statement that it ought to exist. **Follow-up (not Phase 1):** let
`LIVESTACK_BROKER_URL` be a comma list so nodes announce to both their host
broker and the fleet broker.

Suspect/mia thresholds are longer than a host broker's (45/600) because
cross-mesh probes to Nanjing take 0.5–1.5 s and DERP hiccups are routine; a
fleet broker that flaps every peer on every hiccup is noise.

**Deployment prerequisite — the diagnosis in this paragraph was WRONG, and the
real cause is worse.** xc-mac-studio's polytts does not bind `127.0.0.1`: its
`server.py` has called `uvicorn.run(app, host="0.0.0.0", port=PORT)` all along.
The reason the fleet cannot see it is that the process **never finishes
starting**. Measured 2026-09-05: PID 96850, 12 h 20 m elapsed, **3.17 seconds of
CPU**, log untouched for 12 hours, stopped at `Loading MLX model
Qwen3-TTS-12Hz-1.7B-Base-8bit …` — blocked, not working. (The elapsed-vs-CPU
comparison is benchday's 90-second wedge triage, `docs/daemon-startup-wedge-
diagnosis.md`; it applies verbatim here.) Its log also shows `[livestack]
reported for duty … as http://127.0.0.1:8100/livestack` from a process that has
never bound anything — which is exactly what Phase 0.2 stops, and the Mac has
not been redeployed yet.

So this is not a bind to change; it is a wedged service to fix, and it is
recorded in §9a rather than done here. The fleet broker lists it correctly
meanwhile: a row, `suspect` then `mia`, with `Connection refused`.

### 3.3 `GET /fleet` shape

```json
{
  "dispatch": false,
  "peers": 9,
  "generated_at": 1788600000.0,
  "hosts": {
    "zz-tower0": {
      "nodes": [
        {
          "peer": "http://100.64.0.3:8766/livestack",
          "state": "fresh",
          "unseen_seconds": 3.1,
          "probe_ms": 1089.4,
          "device_id": "zz-tower0/gpu0",
          "kinds": ["polyasr"],
          "ready": true,
          "detail": "resident",
          "units": [{"kind":"asr","resident":true,"busy":false},
                    {"kind":"align","resident":false,"busy":false}],
          "device_mem": {"capacity": 25296044032, "free": 12345678901},
          "load": {"in_flight": 0, "pressure": 0.51, "in_flight_source": "server"}
        }
      ]
    }
  }
}
```

Rules: a peer that cannot be read this instant is **still a row** (state,
age, `last_error`) — an absence is a row, never a gap. `probe_ms` is absent
until measured. `load` is absent when the node reports none.

### 3.4 Verification (the implementer must show these)

1. `curl :8801/fleet` lists **three hosts** and nine nodes; `probe_ms` for
   tower0 nodes is ≥ 10× that of local nodes.

   **Measured 2026-09-05: nine nodes, and SIX hosts, not three.** The node count
   and the distance ratio pass with room — local ~2 ms, xc-mac-studio ~13 ms,
   zz-tower0 ~530 ms, a ratio of ~250x against a bar of 10x. The host count does
   not, and the reason is §9a item 2: `xc-tower-ubuntu-b` and
   `xc-tower-ubuntu-gpu1` are FAKE host ids, so one machine appears as three,
   and one physical card appears as two devices (`xc-tower-ubuntu/4bac2869` and
   `xc-tower-ubuntu-b/4bac2869` — same UUID suffix, because it is the same
   card). The sixth is `unknown`, which is correct: a node that has never been
   snapshotted has no host to be grouped under, and the view says so rather than
   guessing. This verification is what turns the workaround from a note into a
   visible cost.
2. `journalctl -u livestack-fleetd` contains **no** `[hostbroker] evict` or
   `warm` lines over 10 minutes, while the host brokers' journals continue to
   show their own reconcile activity. (Observe-only is proven by absence.)
3. Kill polyasr-b on xc-tower-ubuntu: its row goes `fresh → suspect` within
   60 s and `mia` within 15 min, with `last_error`. Restart it: `fresh` within
   one probe cycle.
4. With 0.1 deployed on the mac's host broker: stop the mac's polyasr with
   `launchctl bootout`, then start a process that only announces (a 10-line
   script POSTing `/peers`) — it must **not** become `fresh`.
5. Every decision-ledger record the fleet broker is required to emit in this
   phase (see `decision-ledger.md` §4.2) appears in its ledger with the fields
   named there.

**Results, 2026-09-05, all five run against the live deployment:**

| # | result |
|---|---|
| 1 | nine nodes; probe_ms 2 ms local / 13 ms mac / 530 ms tower0 = ~250x (bar: 10x). Six hosts, not three — see above |
| 2 | **zero** `evict`/`warm`/`reclaim` lines over a full 10 minutes, while the broker logged five real membership transitions in the same window. Observe-only proven by absence, with the host broker's own journal for contrast |
| 3 | polyasr-b stopped ⇒ `suspect` at 61 s with `Connection refused`; restarted ⇒ `fresh` 30 s later at probe 2.1 ms. The `mia` edge is covered by the mac's genuinely-dead polytts rather than by taking a live ASR engine down for 15 minutes |
| 4 | a process that ONLY announces — 17 announces over 80 s to a port with nothing behind it — went `fresh → suspect` on the normal clock. Registered, never certified |
| 5 | one `observe` record per transition (not per tick: the reconcile loop ran ~120 times), every record schema-valid, each naming the state change and the last probe error |

Running §3.4 also found a defect on the path it exercises: `sweep_leaks` armed
its per-peer throttle *after* touching `peer.leak`, which on a `RestPeer` is a
property that HTTP-GETs `/residence` — so a down peer raised first and the
throttle never armed. The host broker had been printing ~17k "reclaim failed"
lines a day about a chipgen that has been gone for hours. Fixed; the same window
now shows zero.

### 3.5 Tests

`tests/test_hostbroker.py`: `dispatch=False` → `plan_and_apply` returns a
plan with actions and calls no peer `warm`/`evict` (fake peers assert). A
peer whose `units()` sleeps 50 ms yields `probe_ms ≥ 50`; EWMA converges.
`fleet_view()` groups by host, includes an unreachable peer as a row, and
carries `load` from `capability()`.

---

## 4. Phase 2 — links and ranking

### 4.1 Links: from a star to a matrix

Phase 1's `probe_ms` is distance *from the fleet broker*. A client in Nanjing
should not be ranked by Vaughan's view of Nanjing.

**Change:** each **host** broker gains `LIVESTACK_LINK_PEERS` (list of other
hosts' broker URLs). On its reconcile loop it GETs each `/peers` (cheap, no
plan) and records `link_ms[host_id]` (EWMA). It publishes these on `GET
/status` under `"links": {"zz-tower0": 612.0, "xc-mac-studio": 35.2}`. The
fleet broker collects every host's `links` in `fleet_view()` under
`hosts[h].links` — a full matrix, measured, decaying, with *no opinion* for
unmeasured pairs.

Relays matter too: a client off the mesh reaches engines through a relay, so
each relay's distance to each engine is part of the truth. The benchday relay
already probes its targets (`applied.reachable`); expose its per-target probe
latency on its authenticated `/inventory` and let the fleet broker read it
(the hub already reads that endpoint). This is the piece that would have shown
"NA client → LA relay → Nanjing relay → Toronto GPU" as absurd.

### 4.2 Ranking: `GET /fleet/rank?kind=asr&region=na&via=direct|relay:<id>`

Pure function over the fleet view, in a new module `fleet_rank.py` — **no
I/O, injectable clock**, like `planner.py` and `fleet_scheduler.py`. Given
`(kind, client_region, vantage)`:

1. **Filter** — only nodes with `state == fresh` and `ready == true` hosting
   `kind`. Region policy is a **hard filter** applied by the caller (the hub
   knows the account's allowed regions; the fleet broker never decides
   policy).
2. **Order** — lexicographic: (a) distance band from the vantage (`links` or
   `probe_ms`, bucketed: `<50`, `<200`, `<600`, `≥600`, `unknown` last);
   (b) within a band, load ascending (`in_flight` primary; `pressure` may only
   raise; *no opinion* sorts after any opinion but before `unknown` distance);
   (c) then stable by `target_id`.
3. **Emit** — `[{target_id, node, host_id, distance_ms, load, reason}]` where
   `reason` is a string a human can read: `"band<50ms, in_flight=0"`.

Bands, not raw milliseconds: the picker will re-measure the fine detail
itself; the fleet's job is to keep a client from *starting* on the wrong
continent. This also keeps ranking stable under jitter.

**Expiry:** the response carries `generated_at` and `ttl_s` (default 60).
Consumers discard past TTL. A stale ranking is worse than none.

### 4.3 Into the manifest (benchday)

`hub/src/speech_relay.ts` `buildSpeechRouteManifest` currently emits per
target `{target_id, kind, label, region, processing_region, routes[]}`, and
per route a static `priority`. The Dart picker uses `priority` only as a final
tie-break, and `MeshRouteCandidate` carries it.

**Change (hub):** if `BENCHDAY_FLEET_BROKER_URL` is set, the hub fetches
`/fleet/rank` for each `(kind, region)` on a 30 s cache, and — **after** its
own region-policy filter — orders `targets[]` by the fleet's order and sets
each route's `priority` from the rank index. Absent/stale/erroring fleet ⇒
today's order, unchanged. The manifest gains `"fleet_rank": {"generated_at",
"ttl_s", "vantage"}` so a client and the ledger can tell which order they got.

**Change (client):** none required — `priority` already flows into the
picker. Optionally, `MeshRoutePicker` could accept the fleet order as its
*bootstrap* order so the pessimistic-bootstrap rule ("an unmeasured node
cannot dethrone a proven one") is seeded correctly; that is a follow-up.

**Hub reachability:** public-la is not on the tailnet, so it cannot reach
`:8801` on xc-tower-ubuntu. Options, decided by the owner: (a) put the hub on
the tailnet (the architecture doc already argues for it); (b) have the fleet
broker *push* rankings to the hub over the daemon's authenticated outbound
channel, the way speech-capacity announces already travel. (b) reuses a path
that exists and keeps the hub off the mesh; prefer it unless (a) is decided
for other reasons.

### 4.4 Verification

- `GET /fleet/rank?kind=asr&region=na` from xc-tower-ubuntu lists local
  engines first, the mac second, tower0 last; from tower0's vantage
  (`via=host:zz-tower0`) the order inverts.
- With one local engine saturated (`tools/lb-check` at concurrency 8), the
  ranking within the local band flips to the idle one within one TTL.
- Manifest from the hub for an NA account carries `fleet_rank` and `priority`
  matching the rank; for an account whose region policy excludes `na`, the
  rank cannot reintroduce an excluded target (test with a fake fleet response
  containing one).
- Every ranking emits a ledger record (`decision-ledger.md` §4.2).

**Results, 2026-09-05.** The links matrix is live and measured across all three
GPU hosts. It is asymmetric, which is the whole reason a matrix beats a star:

| from ↓ / to → | xc-tower-ubuntu | xc-mac-studio | zz-tower0 |
|---|---|---|---|
| **xc-tower-ubuntu** | — | 16 ms | 1554 ms |
| **xc-mac-studio** | 12 ms | — | 767 ms |
| **zz-tower0** | 605 ms | 950 ms | — |

Vantage inversion, verified: from `direct` the order is
`18:8766, 18:8767, 2:8765, 3:8766`; from `via=host:zz-tower0` it is
`3:8766 (0 ms), 18:8766 (763 ms), 2:8765 (1393 ms)`. Saturation, verified: with
`:8766` held at `in_flight=6`, it ranked **below** the idle `:8767` in the same
band, and returned above it when the burst drained.

Three things the live run showed that the design did not anticipate:

1. **The resting-pressure comparison is not device-comparable, and it currently
   decides.** Every sample above ranks xc-mac-studio FIRST — 21 ms away — over
   two engines 2 ms away, because `in_flight` ties at 0 and pressure then
   decides: 0.156 on Apple unified memory against 0.6501 on a 3090 holding
   resident models. §4.2's own prose already knows the shape ("two idle engines
   holding the same model on **identical cards** reported byte-identical
   pressure") but the formula `max(in_flight/8, pressure)` is applied across
   *different* hardware, where resting pressure measures how much model is
   loaded, not any capacity to serve. The client picker is protected from this
   by `exploreBand` — load only breaks a NEAR-tie in *latency* terms — and a
   distance BAND is far wider than that (0–50 ms). **Recommendation for the
   owner, not applied here because §4.2 is settled:** either gate the pressure
   term the way the picker does (a ratio band inside the distance band), or use
   pressure only when it exceeds a contention threshold. Implemented as
   specified meanwhile, so the behaviour above is the design's, not a bug in it.
2. **`LIVESTACK_CAPABILITY_TTL` bounds how fast a ranking can react**, and 15 s
   is long next to a ~1 s transcription: catching the saturation flip needed a
   sample taken inside the burst. That is not a defect — it is the reason §10's
   first line holds, that this is not a replacement for the client picker — but
   it should be stated where someone reads the ranking rather than inferred.
3. **A fake `host_id` makes an engine unrankable from any remote vantage.**
   `100.64.0.18:8767` reports host `xc-tower-ubuntu-b`, no host has a link row
   to that name, so from `host:zz-tower0` or `host:xc-mac-studio` it scores
   `bandunknown` and sorts last — behind engines on the other side of the
   Pacific. §9a item 2 costs more than a fragmented view.

---

## 5. Phase 3 — job admission ("throw a task at it")

### 5.1 The surface

`POST /fleet/admit`

```json
{"kind": "align", "sla": "batch", "owner": "media-corpus",
 "selector": {"arch": "cuda"}, "locality_host": "zz-tower0",
 "estimate": {"duration_s": 40}}
```
→
```json
{"granted": true, "target": {"host_id": "xc-tower-ubuntu",
 "device_id": "xc-tower-ubuntu/3f9a…", "node": "http://100.64.0.18:8766/livestack"},
 "lease_id": "…", "reason": "resident, in_flight=0, band<50ms; zz-tower0 had align non-resident and pressure 0.81",
 "decision_id": "…"}
```

`sla` ∈ `interactive | normal | batch` maps to `fleet_scheduler.Sla`. The
response names the **node facade** so the caller can go straight to it. The
caller heartbeats the lease (existing `/lease/{id}/heartbeat`) and releases it;
a dead caller's lease expires (`LIVESTACK_LEASE_TTL_S`). The ledger records
every admit with the full candidate set and the reason each lost.

### 5.2 How it decides

`fleet_scheduler.schedule()` already exists and is pure. Build its
`FleetState` from `fleet_view()`: each fleet device becomes a `Target`
(`tier=LOCAL`, `capacity` from measured free + concurrency, `running=True`,
`labels` from the node's capability labels + `{"region": ..., "host_id": ...}`).
Add one term: **`distance_ms`** on `Target` (from links/probe) and a
`w_distance` weight in `Weights` so nearer wins among feasible targets. That
is the only scheduler change; cost/deadline/burst logic is untouched and the
RunPod/Aliyun tiers remain available for elastic overflow exactly as designed.

Placement *within* the chosen host (is `align` resident? will loading it
evict something?) stays with that host's broker: the fleet broker's answer
is "go to host H, node N"; the caller's request to N triggers N's own
`manager.ensure` → host-broker `/admit` as today. Two brains, unchanged.

### 5.3 Wiring the first real consumer (benchday)

`hub/src/execution/backend.ts` mode `livestack-gateway` runs jobs through the
shared Livestack gateway (job specs like `unchain.asr`). The fleet broker is
*not* a second gateway — it answers *where*, the gateway answers *how*. First
consumer is the one with recurring, real GPU work and no latency sensitivity:
**media-corpus digests** (`~/xc-setup/media-corpus/auto-ingest.sh`, every 35
min on tower0). Its transcribe/align/digest steps call `/fleet/admit` and use
the returned node. That is the workload that makes an idle card earn its keep
and relieves tower0's single 3090 at the same time.

### 5.4 Verification

- An `align` job admitted while tower0's card is at 0.8 pressure and
  xc-tower-ubuntu's is at 0.2 lands on xc-tower-ubuntu, and the ledger
  record's `reason` says why, naming tower0's pressure.
- The same job with `sla: interactive` and `locality_host: zz-tower0` from a
  Nanjing caller lands on tower0 (distance dominates within feasibility).
- Kill the fleet broker mid-run: the caller's fallback (local-direct or the
  configured gateway) runs the job; nothing is lost; the ledger on the caller
  side records `fleet_unavailable`.

---

## 6. Phase 4 — announce to the fleet; retire the seed list

`LIVESTACK_BROKER_URL` accepts a comma list. Nodes announce to their host
broker and the fleet broker. With 0.1/0.2 in place an announce is discovery
only, so this is safe. Seeds stay as the operator's statement of what ought
to exist; the fleet's `LIVESTACK_PEERS` shrinks to the nodes an operator wants
reported as *missing* when absent.

## 7. Phase 5 — cross-host warm (never evict)

Only after Phases 1–3 have run clean for a period the owner sets. The fleet
broker may `POST {host_broker}/warm {kind}` — asking the **host broker**, not
a node — ahead of predicted demand (e.g. warm `align` on the idle host before
the 35-minute digest run). The host broker applies its own planner and may
refuse. The fleet broker never evicts anything anywhere; the blast radius of a
wrong eviction is another host's live engine.

---

## 8. Decisions — made by the owner 2026-09-05

Implementers: these are settled. Do not re-open them in a PR; if one turns
out to be wrong, say so in the PR and let the owner re-decide.

1. **Hub ↔ fleet path: the fleet broker PUSHES rankings to the hub over the
   daemon's authenticated outbound channel** — the road speech-capacity
   announces already travel. The hub stays off the tailnet. Concretely: the
   xc-tower-ubuntu daemon gains a `fleet_rank` message (same shape as the
   `/fleet/rank` response, one per `(kind, region)`, on the rank TTL cadence)
   that the hub stores in memory with `generated_at`/`ttl_s` and the manifest
   builder reads. A daemon that stops sending leaves the hub with an expired
   ranking, which it discards. Putting the hub on the tailnet is NOT part of
   this work.
2. **Fleet broker host: xc-tower-ubuntu.** Outside the GFW, reaches every
   node directly. Phase 2's links matrix removes the single-vantage bias.
3. **Ranking TTL: 60 s.** A starting number, not a principle. A stale ranking
   costs one bad first guess, because the client picker still probes and
   fails over. Tune from ledger findings (`decided_on_stale_inputs`), not
   from intuition.
4. **Phase 5 is deferred.** Phases 1–3 run first; the owner decides later
   whether advisory-only is already enough and, if not, what "clean" means
   before the fleet may warm across hosts. Never evict, regardless.
5. **Region grants are STALE and must be fixed in Phase 0 — this was
   "confirm" and turned out to be "fix."** On the hub,
   `/etc/benchday/hub.env` line 60:

       BENCHDAY_SPEECH_TARGET_REGIONS=qwen-sg=apac-sg,zz-tower0-asr=asia-cn,xc-mac-studio-asr=asia-cn,xc-mac-studio-tts=asia-cn,zz-tower0-tts=asia-cn

   labels the Mac's engines `asia-cn` from before it moved to Toronto
   (2026-08), and does not list `xc-tower-ubuntu-asr` /
   `xc-tower-ubuntu-gpu1-asr` at all, so `engineRegion()` in
   `hub/src/speech_relay.ts` gives them the default — also `asia-cn`. Every
   Toronto-area engine is currently granted as China. They do compete as a
   pair, but under the wrong policy region: an NA account whose policy
   excludes `asia-cn` cannot reach its own local engines, and a CN account
   can be routed to Toronto. **Required change (operator env, hub restart):**

       BENCHDAY_SPEECH_TARGET_REGIONS=qwen-sg=apac-sg,zz-tower0-asr=asia-cn,zz-tower0-tts=asia-cn,xc-mac-studio-asr=na,xc-mac-studio-tts=na,xc-tower-ubuntu-asr=na,xc-tower-ubuntu-gpu1-asr=na

   Add this as Phase 0 item **0.7** in the handoff list. Region is operator
   policy, so this is the operator's line to change — recorded here, not
   changed by the architect. Verify with `GET /v1/speech/capacity`
   (authenticated): each of the four should show `processing_region: na`.

   **APPLIED 2026-09-05** on `root@benchday.zztech.io`, hub restarted, previous
   file kept at `/etc/benchday/hub.env.bak-region-grants-20260905-093116`. One
   correction to the verification step: the grants surface on **`GET
   /v1/speech/routes`**, not `/v1/speech/capacity` — the latter is the
   daemon-announced capacity roster, a different mechanism whose rows carry
   `processingRegion: "own"` and are unaffected by this env. Measured after the
   restart: `xc-mac-studio-asr`, `xc-mac-studio-tts`, `xc-tower-ubuntu-asr` and
   `xc-tower-ubuntu-gpu1-asr` all `na`; `zz-tower0-*` `asia-cn`; `qwen-sg`
   `apac-sg`.

## 9. Handoff — task list for implementing agents

Each is one PR; each names its tests and its ledger obligation.

- [x] 0.1 membership: announce registers, snapshot certifies — `membership.py`, `peer-membership.md`, tests
- [x] 0.2 registrar self-probe — `announce.py`, new `test_announce.py`
- [x] 0.3 node-supplied `device_id`; units keyed `(kind, peer)` — `facade.py`, `serve.py`, `hostbroker.py`, tests; migrate polyasr/polytts/harmony-llm units to pass nothing (derived) and verify
- [x] 0.4 `in_flight` contract + `in_flight_source` — `serve.py`, `facade.py`, migrate four servers, tests
- [x] 0.5 meter/device agreement — `meters.py`, `serve.py`, tests
- [x] 0.6 port picker fixes to `route.rs` + wasm — Rust tests ported from `load_distribution_test.dart`
- [x] 0.7 fix stale region grants on the hub (§8.5): Mac + xc-tower-ubuntu engines → `na`; hub restart; verify via `/v1/speech/capacity`. Operator env, one line, must precede Phase 2 or the ranking will be filtered by the wrong policy
- [x] 1 fleet broker observe mode + `probe_ms` + `capability()` + `/fleet` — `hostbroker.py`, `hostd.py`, tests; **ledger emitter** (`decision-ledger.md` §4.2); deploy `livestack-fleetd` on xc-tower-ubuntu; open mac polytts bind; run §3.4
- [x] 2a links matrix — host brokers `LIVESTACK_LINK_PEERS`, `/status.links`; relay per-target probe latency on `/inventory`
- [x] 2b `fleet_rank.py` + `GET /fleet/rank` — pure, tests; **ledger emitter**
- [ ] 2c hub consumes rank into manifest (benchday `speech_relay.ts`), region filter stays hub-side, `fleet_rank` field, tests with fake fleet; decide push vs tailnet
- [ ] 3a `distance_ms` + `w_distance` in `fleet_scheduler.py`; `FleetState` from `fleet_view()`; `POST /fleet/admit`; tests; **ledger emitter**
- [ ] 3b media-corpus digest steps call `/fleet/admit`; fallback path; caller-side ledger record on `fleet_unavailable`
- [ ] 4 multi-URL `LIVESTACK_BROKER_URL`
- [ ] 5 warm-only cross-host, gated on owner's decision
- [ ] docs: `HARMONY.md` gains a "Fleet" section; benchday `docs/route-load-balancing.md` points here; storage-bounds inventory rows for every new store (`decision-ledger.md` §6)

## 9a. What Phase 0 exposed (2026-09-05) — open, not fixed here

Implementing 0.3/0.4/0.7 made three pre-existing facts visible. None is in the
scope of the items that revealed them, and each is an operator decision.

1. **`polyasr-b` is on card 0, not card 1.** Its systemd unit is described as
   "polyasr server B (CUDA cuda:1) — xc-tower-ubuntu GPU1" and sets
   `CUDA_VISIBLE_DEVICES=0`. With derived device ids this is now legible rather
   than inferred: 8766 and 8767 both report `xc-tower-ubuntu/4bac2869`, while
   harmony-llm on 8188 reports `.../a46c4c2e`. So the two ASR engines share one
   3090 and the second card holds only the LLM — which is the opposite of what
   the fleet's own naming says, and it means "two engines compete as a pair"
   (§8.5) is true of latency but not of capacity. The hub compounds the naming:
   `BENCHDAY_ASR_TARGETS` calls 8767 `xc-tower-ubuntu-gpu1-asr`.
2. **The fake `host_id`s can now be retired, but they are load-bearing.**
   `POLYASR_HOST_ID=xc-tower-ubuntu-b` and
   `HARMONY_LLM_HOST_ID=xc-tower-ubuntu-gpu1` were workarounds for the
   `{host_id}/gpu0` template and are no longer needed for device identity. They
   are NOT free to change: the hub's speech target ids and the region grants
   fixed in 0.7 are keyed to those names. Retiring them is one coordinated
   operator change, not a node-side edit.
3. **There is no `xc-tower-ubuntu-tts` speech target.** `BENCHDAY_TTS_TARGETS`
   is unset on the hub, so `parseDirectTtsTargets` falls back to its two-node
   default (`xc-mac-studio-tts`, `zz-tower0-tts`) — and polytts has been running
   on xc-tower-ubuntu, unroutable, the whole time. Meanwhile the Mac's polytts
   is reported `unreachable: ...127.0.0.1:8100` by its own daemon, i.e. not
   running at all, which makes `zz-tower0-tts` the only live TTS target for an
   NA account whose policy excludes `asia-cn`.

## 10. What this deliberately is not

- Not a replacement for the client picker. Local probing, quarantine and
  failover are what keep dictation alive through a dead route.
- Not a broker that evicts across hosts.
- Not a source of region. Region is operator policy from the grant.
- Not a second Livestack gateway. The gateway is *how* a job runs; the fleet
  broker is *where*.
