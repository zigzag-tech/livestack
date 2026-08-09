# Peer Membership — Harmony learns who is on the host, and who has gone

**Status:** implemented (MVP, §6); this doc is the design record. Layer below
`resource-planner.md` (placement) and
beside `fleet-scheduler.md` (capacity creation). This one is about *knowing what
exists*.

**North star: starting a model server is the only action required.** No
`LIVESTACK_PEERS` to edit, no broker restart, no operator step. A node that runs
is a node Harmony arbitrates; a node that stops being there stops being counted.
Every decision below is measured against that sentence.

---

## 1. What was broken

Before this change, `HostBroker` took a **static** peer list. `hostd` read
`LIVESTACK_PEERS`, and when unset fell back to three hardcoded localhost URLs
(polyasr 8766, polytts 8100, chipgen 8844). Two failures fell straight out of
that:

**A node that existed was invisible.** Start a fourth model server and Harmony
never learned of it. Its VRAM was real and its units unplannable — the planner
saw the memory disappear from `measured_free` with nothing to attribute it to,
which is precisely the condition that made it shed innocent units.

**A node that was gone was polled forever, silently.** `snapshot()` caught the
per-peer exception, logged one line, and continued. There was no record that a
peer had been absent for three weeks rather than three seconds, and nothing
surfaced the difference.

Measured on xc-mac-studio, 2026-08-08, while polytts was down:

```
92,089 × "[hostbroker] peer unreachable, skipping this cycle: Connection refused"
9.2 MB of hostd log, one line every 5 s, for weeks
```

Harmony was arbitrating a single peer and reporting nothing wrong. The port in
that host's `LIVESTACK_PEERS` was also stale relative to the doc default (8765
vs 8766) — a fact nobody could have noticed, because a misconfigured peer and a
dead one produce identical output.

The static list was the cause of both. It was a second, hand-maintained copy of
a fact the nodes already knew about themselves.

Both are fixed as designed below: `HostBroker` owns a `PeerRoster` with dynamic
`register_url()` (hostbroker.py:100,224), and `snapshot()` probes absent peers
on the roster's `due_for_probe` backoff and records failures through
`mark_probed`/`mark_seen`, logging on transitions only
(hostbroker.py:302-334).

## 2. The shape: nodes report, the broker ages

Two layers, mirroring the arbiter's own doctrine that **peers are ground truth
and the broker keeps no durable state**.

```
  node startup ──register──▶ ┌──────────────┐
  node heartbeat ─renew────▶ │  HostBroker  │  roster: peer -> last_seen, source
                             │   (soft)     │  fresh ─45s─▶ suspect ─10m─▶ mia
  LIVESTACK_PEERS ──seed───▶ └──────────────┘         ◀── any success
                                    │
                                    ▼  fresh peers only
                              plan(WorldState)
```

### Layer 1 — report for duty

`attach()` gains self-registration. On startup, and then on an interval, a node
POSTs to the host broker:

```
POST /peers  {"facade_url", "host_id", "device_id", "kinds": [...], "readiness": {...}}
```

The server accepts all five fields (hostd.py:148-159); the announcer currently
sends only `facade_url`, `host_id` and `kinds` (announce.py:41-45) — `device_id`
and `readiness` are accepted but not yet sent.

Zero configuration, because neither side needs to be told anything it does not
already know:

- the node knows its own facade URL — it is serving it;
- the broker's address is a host constant with a working default
  (`LIVESTACK_BROKER_URL`, default `http://127.0.0.1:8799`);
- registration is **idempotent on `facade_url`**, so a restart re-registers
  rather than duplicating.

The node **retries until it succeeds**, so start order does not matter. A broker
that starts after its nodes, or restarts, has an empty roster that refills
within one heartbeat — which is the same soft-state property the broker already
claims for placements, extended to membership.

`LIVESTACK_PEERS` is kept, demoted to **seeds**: it still works, so no existing
deployment breaks and a node too old to self-register is still arbitrated. It is
simply never *required* any more.

### Layer 2 — mark MIA

The broker records `last_seen` per peer — advanced by a successful snapshot *or*
a registration renewal — and derives state from elapsed wall-clock, not from
counted cycles:

| State | When | Effect |
|---|---|---|
| `fresh` | seen within the suspect threshold | polled every cycle, planned over |
| `suspect` | not seen since | polling backs off, still in `/status`, not planned over |
| `mia` | not seen for the MIA threshold | probed rarely; a **registered** peer is pruned, a **seeded** peer is kept |

Three rules earn their place:

**Any single success returns a peer to `fresh` immediately.** Membership is a
report, not a promise — the same asymmetry the placement planner already uses
for restore.

**Log on transition, never per cycle.** The 92,089 lines were one line of
information printed 92,089 times. `fresh→suspect`, `suspect→mia` and
`mia→fresh` are events; "still gone" is not.

**Seeded peers are never pruned; registered peers are.** An operator writing a
seed is a statement that the node ought to exist, and deleting it would silently
undo their intent. A self-registered peer, by contrast, will re-register the
moment it returns, so pruning costs nothing and keeps the roster bounded.

### Backoff, and why it is not just politeness

A dead peer currently costs a full connect attempt every reconcile tick. Backing
`suspect`/`mia` peers off to a slow probe is what makes it *safe* for the roster
to hold peers that are not there — which is what lets us keep seeds forever and
prune only lazily. Without backoff, tolerance of absent peers is a cost; with
it, it is free.

## 3. What the planner sees

Unchanged in shape: only `fresh` peers contribute units and placements, exactly
as only reachable peers do today. The existing guarantee stays load-bearing and
must not regress — *a peer that is down must not blind the whole arbiter*,
because the surviving peers' driver-level `measured_free` still counts the
absent process's VRAM, so aborting the snapshot would fail open into an OOM.

What changes is that "not contributing" becomes a *state with a duration*
instead of an exception swallowed per cycle.

## 4. Bounds

Per repo practice, anything that grows with uptime needs a ceiling:

- roster entries are capped, and registration beyond the cap is refused under a
  named reason rather than growing;
- `mia` registered peers are pruned on a bounded window; an unset window means
  **disabled**, never a default that deletes;
- transition-only logging is itself the log bound — the current failure mode is
  a log that grows linearly with downtime.

## 5. Rejected

**Port scanning / mDNS discovery on the host.** Tempting for zero-config, but it
infers membership from a listening socket, and an open port is not a statement
that a process wants to be arbitrated. Registration is an intent; a scan is a
guess. It also invents a second discovery mechanism when the node already has a
reason to call the broker.

**Making the broker the registry of record with durable state.** Contradicts the
existing doctrine and buys nothing: the nodes are the ground truth, and a broker
that must be backed up is a broker that can be wrong.

**Removing `LIVESTACK_PEERS`.** Breaks running deployments for no gain. Seeds
cost one branch and preserve the operator's ability to say "this ought to be
here" — which is exactly the signal that distinguishes a pruneable peer from one
worth reporting as missing.

## 6. Scope

**MVP:** roster with `last_seen` + three states + transition logging + backoff;
`POST /peers` registration; `attach()` self-register with retry; seeds kept;
`/status` reports state and age. Tests mirror `test_hostbroker.py`.

**Deferred:** cross-host registration (a node registering with a broker on
another machine), which is the federation question and belongs with
`fleet-scheduler.md`.
