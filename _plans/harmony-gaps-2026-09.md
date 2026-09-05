# Harmony — what is lacking, from a week of running it on real hardware

**Status:** proposal / punch list. Written 2026-09-05 after enlisting
xc-tower-ubuntu (2× RTX 3090) into the fleet, standing up four Harmony nodes on
it, and load-testing route selection against two live engines. Every item below
names the evidence that produced it; nothing here is speculative.

**The one-line verdict.** Harmony today is a *GPU residency arbiter for one
host*. Its memory model is strong and the per-host arbitration is correct — it
evicted an over-budget LLM within seconds, exactly as designed. It is **not** a
fleet placement brain: it has no fleet view, no latency or topology model, and
region is a label nothing reads. The thing that actually decides where speech
work goes is the client-side route picker, a separate system. Anyone expecting
"Harmony figures out the fleet by itself" will be surprised the way we were
when a second engine received zero traffic.

Ordered by *evidence strength × cost*, not by ambition.

**Answered by two design documents (2026-09-05):** `fleet-broker.md` — the
phased architecture that closes Tiers 1–3, with per-phase code touchpoints,
tests, deployment and verification; and `decision-ledger.md` — the
retrospection requirement that every phase must satisfy, so a future agent
can read a routing decision and say where it should have gone instead.
Implementation is handed off from those two; this file stays as the evidence.

---

## Tier 1 — defects with a reproduction, small to fix

### 1.1 An announce is not proof of life

`membership._upsert` says it outright — *"Registration IS proof of life — it
arrived, so the node is there"* — and calls `mark_seen()` on every announce.
But the registrar is a thread **inside the engine process**. It keeps POSTing
every 30 s whether or not uvicorn ever bound its port.

**Evidence:** xc-mac-studio polyasr, 2026-09-05. Process alive 7 h with 3 s of
CPU, nothing listening on :8765, wedged in model load before bind. Its broker
listed it `fresh, seen 7.7 s ago` the entire time. `sample` showed the main
thread blocked while two `livestack-registrar` threads announced. NA had no
working ASR engine and nothing said so.

**Fix:** an announce *registers* (creates/keeps the record, so a new node is
discovered immediately) but does not *certify*. `mark_seen` moves to the
broker's own successful `/residence` snapshot. Seeds are unchanged. This
inverts one sentence of `peer-membership.md`; it is the right sentence to
invert, because "membership is a report, not a promise" was meant to tolerate
*absence*, not to certify a corpse.

### 1.2 The registrar must not announce a port that is not listening

Sibling of 1.1, on the node side. `start_registrar` is called from `attach()`
at import time, before uvicorn binds. It should self-probe
`http://127.0.0.1:{port}{prefix}/health` and announce only once that answers.
Cheap, and it would have made the mac's wedge visible as *absent* within a
minute instead of *fresh* for seven hours.

### 1.3 Route-core drift — the Dart picker has fixes `route.rs` does not

`route-selection.md` is explicit: *"Any behavioural change must land in both or
in neither."* On 2026-09-05 three behavioural changes landed in benchday's
`packages/mesh_route` and **not** in `shared/src/route.rs`:

- load-aware near-tie breaking (`loadAware`, `recordLoad`, queue depth primary,
  pressure can only raise the estimate);
- forced re-measurement of an overdue candidate (`remeasureAfter`, backoff), so
  a demoted engine is not exiled by `explore_band`;
- a re-measured sample **replaces** the stale EWMA instead of smoothing into it.

**Evidence:** measured on two live 3090s, 80 requests at concurrency 8:
`0% / 100%` before, `44% / 56%` after. The conformance corpus that keeps the two
implementations honest now has cases Rust cannot pass.

**Fix:** port all three to `route.rs` with the same test cases
(`load_distribution_test.dart` → Rust), then the wasm binding follows for free.
This is the cheapest item on the list and the only one that is a doctrine
violation rather than a gap.

### 1.4 `device_id` is `f"{host_id}/gpu0"`, hardcoded

The facade cannot express a second card on one host, or two nodes on one card,
without lying about one of them.

**Evidence:** on xc-tower-ubuntu a second polyasr on card 1 needed a *fake*
host_id to get its own device. When it was later moved to card 0, the stale
host_id left Harmony modelling a 5 GB ASR engine and a 20 GB LLM as
co-resident on one 24 GB device — and it evicted the LLM seconds after load
(`relieve measured over-budget pressure`). Correct arbitration, false topology.
Two ASR engines that genuinely share card 0 are still modelled as two devices,
so the static budget double-counts that card.

**Fix:** the node reports its real device identity — CUDA device UUID, MLX
`device_info`, or an operator label — and the broker keys devices by that.
Units are keyed by `(kind, node)` rather than `kind`, so two polyasr on one
card are two units on one device, which is the truth.

### 1.5 Lease-derived `in_flight` cannot see real work

`/livestack/capability` → `load.in_flight` counts non-`__usage__` leases. The
batch and websocket ASR paths reach the model through `manager.ensure`, which
takes a `__usage__` lease — correctly excluded, because it lives for the whole
idle-evict TTL and marks recency, not work.

**Evidence:** six concurrent transcriptions, `in_flight` stuck at 0, while
`pressure` moved 0.1974 → 0.2028 (under half a percent — ASR activations are
small against 24 GB). polyasr now counts its own requests and supplies them
via `readiness()`, and the facade merges them.

**Fix:** make that the contract rather than a workaround: `attach()` takes an
optional `in_flight: Callable[[], int]` (or a context manager the server wraps
handlers in) so every node kind reports work the same way, and a node that
does not is flagged as *no opinion*, never *idle*.

### 1.6 A process's GPU is not automatically the meter's GPU

Three separate bugs in one session came from one root: `cuda_meter` defaulted
to device 0; polyasr selected a card with `.to('cuda:1')` without
`torch.cuda.set_device()`; the LLM wrapper set `CUDA_VISIBLE_DEVICES` on its
child but not on the process running the meter. Each time, two nodes on
different cards reported byte-identical pressure. `cuda_meter` now follows
`current_device()`, but the other two are deployment discipline.

**Fix:** `auto_meter()` should *assert* it agrees with the node's declared
device (1.4) and refuse to report pressure otherwise — a meter that reads the
wrong card is worse than none, because it is confident.

---

## Tier 2 — the fleet view (medium; unblocks everything in Tier 3)

### 2.1 There is no fleet

Each machine runs its own `hostd` seeded with three **localhost** URLs.
`build_broker(peer_urls=...)` already accepts remote peers and `hostbroker`
federates across `host_id`s — the capability exists and nothing exercises it.
tower0's broker does not know xc-tower-ubuntu exists. Every "fleet" decision
today is a client probing engines one by one.

**Fix:** one of

- a **fleet broker** (peerless `hostd` like `livestack-buildd`) whose peers are
  every host's local broker, reached over the mesh; or
- each host broker learning the others via the benchday daemon, which already
  holds the authenticated mesh roster and per-host grants.

The second reuses a source of truth that exists and already enforces region
policy; the first is simpler and self-contained. Either way the `/peers`
membership machinery (1.1 fixed) is what keeps it honest.

### 2.2 Nothing measures the network

There is no RTT, ping, latency, geography or region model anywhere in
`livestack_node`. The only spatial notion is `locality_host == host_id` with a
flat `locality_penalty = 2.0` — data gravity, not topology. `provision_latency_s`
is cloud spin-up time.

**Evidence:** tower2 → xc-tower-ubuntu is 638 ms; the public relay reaches
every mesh engine via Nanjing, so an NA client relaying to an NA GPU crosses the
Pacific twice. Nothing in Harmony can see that.

**Fix:** on the reconcile loop each broker measures RTT to each fleet peer and
to each relay it knows, and publishes `links: {peer_id: rtt_ms}` on its status.
Measured, decayed, with the same *no opinion never idle* rule as load. This is
new state with a natural home once 2.1 exists.

### 2.3 Region is a string nothing reads

`region` is passed through the readiness descriptor and ignored. benchday's
speech-capacity runbook is right that region is **operator policy** (it decides
which accounts may be routed where) and must come from the grant, never the
node. That constraint should be honored, not bypassed.

**Fix:** devices carry an operator-assigned `region`; requests carry
`region_affinity`; the planner applies it as a hard filter for policy regions
and a soft cost for preference. Region never comes from a node's self-report.

---

## Tier 3 — placement that routes interactive work (large; a design decision)

### 3.1 Two brains, and the doctrine that keeps them apart

`route-selection.md` is explicit that *client → endpoint selection is not
hub → worker placement* and the two must not share a scoring model. That is
correct and should survive. `fleet_scheduler.py` is the cross-target brain, but
it answers "run local, burst to Aliyun, or last-resort RunPod, and when is that
worth paying for" — cost, deadline, elasticity. It has no latency term and is
not wired into benchday at all.

**Proposal:** do not turn either into the other. Add a thin third thing:

> **A fleet ranking.** The fleet broker (2.1) computes, per client region, a
> ranked candidate list from load (1.5), links (2.2) and region policy (2.3),
> and publishes it into the speech manifest the hub already serves. The client
> picker keeps doing what it is good at — local probing, failover, quarantine —
> but starts from a fleet-informed order instead of a static priority integer.

That gives the "figures it out itself" behaviour without merging the brains:
the fleet says *where work should go*, the client verifies *whether it can*.
It also degrades safely — no fleet ranking means today's behaviour exactly.

### 3.2 What to decide before building 3.1

- Who owns the fleet broker: livestack (`hostd` mode) or benchday (the hub)?
  The hub is not on the mesh today, which argues for livestack.
- Is a stale fleet ranking worse than none? (Yes — it should expire, like the
  relay's applied snapshot does.)
- Does the ranking carry load at all, or only topology, leaving load to the
  client's fresh probe? Probably the latter: load changes in seconds, the
  ranking refreshes in tens of seconds, and the picker already reads live load.

---

## What is deliberately not on this list

- Making Harmony do cross-host **eviction**. A broker that can evict on another
  host is a broker that can take down another host's engine; the blast radius
  is not worth it until 2.1 has run clean for a while.
- Replacing the client picker with a server decision. The picker's failover and
  quarantine are what make dictation survive a bad route; centralising that
  reintroduces the single point of failure the mesh design removed.
