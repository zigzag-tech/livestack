# RESTful Model Node — unifying GPU service residence under Livestack

**Status:** design / discussion. No code yet.
**Goal:** make every GPU model web server (polyasr, polytts, ollama, chipgen, …)
a self-similar Livestack *node* so that VRAM stays as free as possible —
resident only what is actively leased or explicitly pinned — while each server
still runs as a plain open-source web server with **no startup dependency on a
gateway that may not exist**.

---

## 1. The core idea

A standalone model web server is the **degenerate single-node + embedded-gateway
case of Livestack.** It is not a "dumb localService" babysat by a sidecar; it
*is* a Livestack node that happens to also host its own lease store. The same
abstraction runs at three scales:

```
 standalone (open source)        per-host gateway              mesh
 ┌───────────────────────┐       ┌─────────────────┐           ┌─────────┐
 │ server                │       │ host gateway     │           │ gateway │
 │  ├ runtime (models)   │       │  ├ polyasr node  │           │   ├ … │
 │  ├ residence ctrl     │       │  ├ polytts node  │  ◀──fed──▶ │ hostGW│
 │  ├ embedded lease mgr │  ◀──▶ │  ├ ollama node   │           │ hostGW│
 │  └ REST facade        │       │  └ chipgen node  │           └─────────┘
 └───────────────────────┘       └─────────────────┘
   N = 1, gateway in-process       owns ONE physical GPU's      cross-host
                                   residence ledger             capacity
```

The *only* thing that changes between scales is which `ServiceLeaseManager` the
node is constructed with. Service code is identical.

### Why this is native, not bolted on

`core/src/capabilities.ts` is already a layered, swappable stack:

- `interface CapabilityLeaseStore` (`capabilities.ts:49`) → `InMemoryCapabilityLeaseStore` (`:68`)
- `interface ServiceLeaseManager` (`:296`) → `InMemoryServiceLeaseManager` (`:308`)
  registers capabilities into an in-process store and reaps on a clock
- `PersistedServiceLeaseManager` (`:409`) — a **decorator** wrapping any delegate
  manager + an injectable `ServiceLeaseStore`

The interface + decorator seam is the abstraction layer. "Embedded gateway" =
`InMemoryServiceLeaseManager`. "Mesh node" = a `RemoteServiceLeaseManager` with
the same interface. The gateway itself (`gateway/src/LiveGatewayConn.ts`) is a
thin *connection*, not a heavyweight server — which is what makes co-locating it
inside a model server viable.

---

## 2. Residence = lease semantics (one mechanism, not three)

Today residence is governed by three ad-hoc mechanisms. They collapse into a
single rule:

> **A unit is resident iff it holds ≥1 active lease, or is pinned.**

| Today (bespoke, per server) | Restified (lease semantics) |
|---|---|
| `IDLE_EVICT_SECONDS=180` idle timer (`polyasr_manager.maybe_evict`) | every request auto-acquires/renews a TTL lease; no renewal → reaper evicts |
| `touch()` per WS audio frame (`polyasr/cuda/server.py:1093` dictation path) | **already a lease heartbeat** — the live prototype |
| `IDLE_EVICT_SECONDS=0` (pin) | a **permanent lease** seeded at boot |
| `POST /model/unload` (manual handoff) | `release()` the lease |
| `POLYTTS_IDLE_EVICT_SECONDS`, COLOAD flag | per-unit leases + pin set |

Consequence: "VRAM free except pinned" holds *by construction*. There is no
separate idle subsystem to reason about, and pinning is not a special flag — it
is just a lease that never expires.

### What's missing today

`InMemoryCapabilityLeaseStore.reapExpired` (`capabilities.ts:160`) deletes the
lease *record* and does nothing else — **there is no callback to the runtime, so
no VRAM is freed.** The residence controller (§3, port 3) is the missing bridge.

---

## 3. The abstraction layer: four ports, one per-service

The reusable "restify a model service" kit. Three ports are generic (shipped by
Livestack); only port 4 is implemented per service.

### Port 1 — Lease plane *(exists)*
`ServiceLeaseManager`. Construct with `InMemoryServiceLeaseManager` (embedded) or
a remote/federated manager (mesh). No new work beyond a `RemoteServiceLeaseManager`
for the federated case (phase 3).

### Port 2 — REST facade *(new, generic)*
The uniform, language-neutral HTTP surface every node exposes. REST is the
conformance boundary that lets a Python node and a TS gateway interoperate.

```
GET  /capability                      → WorkerCapability descriptor (kind, units, slots, labels, pins)
GET  /health                          → { status, gpu, residence: ServiceLeaseManager.listActive(), units }
POST /lease                           → acquire { kind, unit?, ttlSeconds? } → { leaseId, expiresAt }
POST /lease/{id}/heartbeat            → extend → { expiresAt }
POST /lease/{id}/release              → release
POST /model/warm   { unit }           → prewarm (optional; reduces cold start)
POST /model/evict  { unit }           → force-evict one unit (NOT all)
POST /model/pin    { unit, pinned }   → seed/remove a permanent lease
```

`polyasr` already ships `GET /health` returning `manager.status()`
(`server.py:675`) and `POST /model/unload` (`server.py:694`). The facade
generalizes these and adds the lease + per-unit verbs.

### Port 3 — Residence controller *(new, generic)*
Subscribes to lease lifecycle and drives the runtime. Encodes the §2 rule:

```
on lease acquired(unit)        → runtime.warm(unit)
on last active lease for unit  → runtime.evict(unit)         # unless pinned
                                  lapses/released
on pin(unit)                   → seed permanent lease; never evict
```

Replaces the dead `reapExpired`-only path with an eviction that actually frees
VRAM, per unit.

### Port 4 — Runtime port *(per-service adapter — the only custom code)*

```ts
interface ModelRuntime {
  warm(unit: string): Promise<void>;
  evict(unit: string): Promise<void>;     // MUST be per-unit
  resident(): { unit: string; pinned: boolean; bytes?: number }[];
}
```

- **polyasr:** `AsrModelManager` already has `load()/unload()/loaded()/status()/touch()`
  per unit, plus `unload_now()` and `maybe_evict()`. **Gap:** `unload_now()` is
  all-or-nothing (`polyasr_manager.py:146`) — must become **per-unit evict** so
  `diarize` can drop while benchday's `asr` stays hot. This is the one
  non-trivial refactor.
- **polytts:** `ModelManager` (qwen/voxcpm, one-in-VRAM) — same shape, same
  per-unit gap.

---

## 4. The central role: per-host GPU gateway

The biggest payoff is at the **host** level. tower0's single RTX 3090 is today
squatted independently by polyasr (~17 GB), polytts, ollama, and chipgen — no
coordination, each with its own or no eviction. Restify all of them and run
**one host-level gateway** that owns *that physical GPU's residence ledger*;
each model server is a node delegating its residence controller to the host
gateway. Eviction becomes arbitrated across all servers: a global VRAM budget,
pin-aware, with cross-service reclamation.

The standalone open-source server is simply the N=1 case where the host gateway
lives inside the lone process. Same abstraction, fractal across scales.

---

## 5. No startup dependency — how the gateway worry is resolved

- The server **never dials out.** In standalone mode the lease manager is an
  in-process object; it always exists, requires no network, cannot fail to
  connect.
- Federation is **inbound and lazy:** when a host/mesh gateway appears, it
  connects *to* the node's REST facade (or the node's manager is swapped for a
  remote-backed one at construction). If the gateway is absent or dies, the node
  keeps running on its embedded manager — identical behavior to today's
  standalone server.
- Open-source users get a fully self-managing GPU web server with zero Livestack
  network deps: implement port 4, instantiate the in-memory manager, mount the
  facade.

---

## 6. Consumers: lease-then-call (with graceful degradation)

The meeting-digest pipeline = **media-corpus**
(`xc-setup/media-corpus/pipeline.py`, runs on tower0; DB/server on
**zz-tower2 @ 100.64.0.12**; routine `agent/src/templates/mediaCorpusPipeline.ts`).
It currently does raw `POST http://127.0.0.1:8766/v1/diarize` and holds no lease
— under lease-gated residence that is a hazard (the unit could be evicted
mid-job). Consumers move to lease-then-call:

```python
with livestack.lease("asr", unit="diarize") as L:   # acquire + auto-heartbeat
    requests.post(f"{POLYASR}/v1/diarize", ...)       # warm + protected
# released → unit becomes eligible for eviction
```

If no lease manager is reachable, `lease()` returns a no-op lease and the call
hits the raw server (which self-manages via its embedded manager). Standalone
correctness is preserved end-to-end.

---

## 7. Language interop (TS core ↔ Python servers)

- Livestack `core` (TS) is the **canonical spec** and what host/mesh gateways run.
- `polyasr`/`polytts` are Python. Ship a small `livestack-node-py` package: a
  faithful port of the ~150-line lease layer + residence controller + REST
  facade. Port 4 adapters wrap the existing Python managers.
- The **REST contract is the shared conformance test** — identical request/response
  vectors run against both the TS and Python implementations so they cannot
  drift. This is the whole reason "restify": REST is the lingua franca between
  heterogeneous services and the gateway.

---

## 8. Caveats / risks

- **Cold start.** Lease-gated residence means the first call after eviction pays
  the reload (polyasr ~17 GB). Mitigate: warm-on-acquire (lease slightly ahead
  of use), generous TTLs, and a pinned hot set for latency-critical units.
- **Dual implementation drift.** Mitigated by the shared REST conformance vectors
  (§7).
- **Per-unit evict refactor** (§3 port 4) is the only substantive runtime change;
  everything else is additive.
- **Thundering reload** if many consumers race after an eviction — the manager's
  existing GPU lock serializes loads; warm-on-acquire smooths it.

---

## 9. Phasing

1. **Contract + embedded node (reference: polyasr).**
   - Define ports 1–4 + REST facade in `core` (TS spec) and `livestack-node-py`.
   - Refactor `AsrModelManager` to per-unit evict; wrap as a `ModelRuntime`.
   - Embedded `InMemoryServiceLeaseManager`; idle-evict re-expressed as TTL leases;
     pins as permanent leases. polyasr runs standalone, self-managing, no network.
2. **Consumer adoption.**
   - `livestack.lease()` Python helper (no-op when no manager); migrate
     media-corpus `pipeline.py` to lease-then-call.
   - Replicate the kit to polytts.
3. **Per-host gateway (tower0).**
   - `RemoteServiceLeaseManager`; stand up one host gateway owning the 3090's
     ledger; register polyasr/polytts/ollama/chipgen nodes; global VRAM budget +
     pin set (e.g. benchday `asr` pinned, `diarize` leasable).
4. **Mesh federation.**
   - Host gateways federate upward for cross-host capacity/selection (ties into
     `_plans/realtime-session-data-plane.md`).

---

## 10. Open decisions

1. **Kit home & name** — contract in livestack `core` + a `livestack-node-py`
   package?
2. **Per-unit residence** — confirm the polyasr/polytts runtime refactor.
3. **Host gateway timing** — design host-gateway-ready, ship polyasr
   standalone-embedded first (phase 1), introduce the tower0 host gateway as
   phase 3?

**Recommendation:** contract-in-core + Python kit; per-unit residence; polyasr as
the reference conformer with the embedded manager first; host gateway phase 3.
