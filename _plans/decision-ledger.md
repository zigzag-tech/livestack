# Decision Ledger — every placement and routing decision leaves enough behind to be second-guessed

**Status:** DESIGN, ready for implementation handoff. 2026-09-05.
**Applies to:** every phase of `fleet-broker.md`, the existing host broker,
and benchday's client-side route picker. Read this before implementing any
of them; each adds an emitter.

**The requirement, in the operator's words:** *most requests should have
sufficient logs for a future agent to look at and say: this task was routed to
this machine, and because it is remote, or less capable, or busier, it should
have been routed to that machine which sat idle at the time.* Logging for
self-reflection, so future agents can improve the system.

---

## 1. What "sufficient" means, precisely

A decision record is sufficient when a reader who was not present can answer
all five of these from the record alone, without a live system:

1. **What was asked** — kind, SLA, who asked, from where (vantage/region).
2. **What was known** — every candidate that was considered, with the inputs
   the decision used *as they were at that instant*: state, readiness,
   distance, load, residency, region. Not a pointer to "the fleet view" —
   the values, because the view will have changed by the time anyone reads
   this.
3. **What was chosen, and why** — the winner and a reason a human can read.
4. **Why each loser lost** — per candidate, the first rule that eliminated
   it or the term that ranked it lower. This is the field that lets the
   reader say "it should have gone to X": if X's row says `filtered:
   state=suspect` the decision was right given what was known; if X's row
   says `ranked lower: load 0.2 vs 0.0` the reader can check whether load
   was measured correctly.
5. **What happened** — the outcome, joined by id: did the request succeed,
   how long did it take, did the client fail over, and to where.

A record that lacks (4) or (5) is a log line, not a decision record. Today's
`[hostbroker] evict llm@…: relieve measured over-budget pressure` has (3)
and nothing else — it says what, not what else was possible.

## 2. The record

One schema for every emitter. JSON, one object per decision, `snake_case`.

```json
{
  "decision_id": "d_01J9…",              // ULID; sortable, unique, carried downstream
  "ts": 1788600000.123,                  // unix seconds, UTC, float
  "emitter": "fleet-broker",             // host-broker | fleet-broker | client-picker | hub-manifest | job-caller
  "emitter_id": "xc-tower-ubuntu:8801",  // which instance
  "kind": "asr",                         // unit kind / capability
  "decision": "rank",                    // rank | admit | pick | evict | load | defer | failover
  "request": {
    "owner": "media-corpus",             // who asked (account/device/service), never a secret
    "sla": "batch",                      // interactive | normal | batch | null
    "vantage": "host:zz-tower0",         // where the asker is: host:<id> | relay:<id> | region:<r> | client
    "region": "asia-cn",                 // asker's region as the emitter knew it
    "policy_regions": ["asia-cn","na"],  // regions the asker was ALLOWED (hub-side only)
    "selector": {"arch": "cuda"},
    "locality_host": null
  },
  "candidates": [                        // EVERY candidate considered, winner included
    {
      "id": "xc-tower-ubuntu-asr",       // target_id / device_id / route key — the emitter's own id
      "host_id": "xc-tower-ubuntu",
      "device_id": "xc-tower-ubuntu/3f9a…",
      "state": "fresh",                  // membership state as known
      "ready": true,
      "distance_ms": 640.2,              // from the vantage; null = unknown
      "distance_band": ">=600",
      "load": {"in_flight": 0, "pressure": 0.21, "source": "server"},   // null = no opinion
      "resident": true,                  // was the unit resident there
      "region": "na",
      "inputs_at": 1788599998.9,         // when these inputs were measured (staleness is visible)
      "outcome": "chosen",               // chosen | ranked | filtered
      "rank": 1,
      "reason": "band>=600; in_flight=0"          // the term that placed it, or
      // "filtered: state=suspect" / "filtered: region not permitted" / "filtered: not ready"
    },
    { "id": "zz-tower0-asr", "...": "...", "outcome": "ranked", "rank": 2,
      "reason": "band<50 but in_flight=3 pressure=0.81" }
  ],
  "chosen": "xc-tower-ubuntu-asr",
  "reason": "nearest fresh candidate with lowest load; zz-tower0 saturated (in_flight=3, pressure=0.81)",
  "ttl_s": 60,                           // for rank: how long this was valid
  "parent_decision_id": null,            // a pick made from a manifest carries the manifest's rank decision id
  "outcome": {                           // filled by a later event joined on decision_id (see §3)
    "status": "ok",                      // ok | failed | failover | timeout | unknown
    "latency_ms": 1350,
    "served_by": "xc-tower-ubuntu-asr",  // may differ from chosen after failover
    "failover_from": null,
    "recorded_at": 1788600001.6
  }
}
```

Rules:

- **Values, not references.** The candidate rows carry the numbers the
  decision used. A reader must never need the live fleet to interpret a
  record.
- **Every candidate, including the ones filtered out first.** "Filtered" is
  a `reason`, not an omission. The mac's 7-hour outage would have appeared
  as `filtered: state=fresh, ready=false` — or, before the membership fix,
  as a *chosen* candidate whose outcome was `failed`, which is exactly the
  record that would have caught the roster lie.
- **`inputs_at` per candidate.** Staleness is a first-class fact. A rank
  built on a load reading 40 s old is a different decision from one built on
  a reading 2 s old, and the retrospective needs to tell them apart.
- **No secrets, no audio, no transcripts.** `owner` is an id. This ledger is
  for engineers to audit routing; it is not the flight recorder.

## 3. Correlation: how the outcome finds its decision

The decision is made in one process; the outcome happens in another, later.
They are joined by `decision_id`, propagated **with the request**:

| hop | carrier |
|---|---|
| fleet `/fleet/rank` → hub manifest | manifest field `fleet_rank.decision_id` |
| hub manifest → client picker | the client stores it with the manifest; its own `pick` record sets `parent_decision_id` |
| client → engine (ASR ws/batch, TTS stream) | HTTP header `X-Livestack-Decision: <id>`; the ws query string carries `decision=<id>` |
| engine → its host broker (lease/usage) | `owner_id` on the lease is `"<owner>@<decision_id>"` — the lease API already carries `owner_id` |
| `/fleet/admit` → job caller → node | response `decision_id`; caller sends the header on the job request |

Each stage that *observes* the outcome (the client on final/failover; the
node on request completion; the job caller on job end) emits an **outcome
event** `{decision_id, status, latency_ms, served_by, failover_from, ts}` to
its own ledger. The retrospective joins on `decision_id` across ledgers. No
emitter waits for another; a decision with no outcome after `ttl_s × 10` is
`outcome.status = unknown`, which is itself a finding (a request that was
never answered, or a pipeline that dropped the header).

## 4. Emitters — who writes what

### 4.1 Host broker (`hostbroker.py`, exists today)

Emits on every `plan_and_apply` that produced an action: one record per
`Evict`/`Load`/`Defer`, `decision` accordingly, candidates = every unit on
the device with its residency/priority/tier and the measured free memory
before and after. `reason` is the planner's existing string (`"relieve
measured over-budget pressure"`, `"preempted by asr (prio 10)"`, `"no device
can fit even with preemption"`, …) — those strings are already good; what is
missing is the candidate rows around them. `Grant` today carries no reason;
give it one (`"resident"` / `"loaded on demand"` / `"after evicting X"`).

### 4.2 Fleet broker (`fleet-broker.md` Phases 1–3)

- Phase 1: emits `decision: observe` **once per membership transition** (a
  node going suspect/mia/fresh) with the node's row — not per tick.
- Phase 2: one `rank` record per `/fleet/rank` call, full candidate set.
- Phase 3: one `admit` record per `/fleet/admit`, full candidate set, and the
  reason each feasible-but-not-chosen target lost (distance, load, cost).

### 4.3 Hub manifest (benchday `speech_relay.ts`)

One record per `/v1/speech/routes` response when a fleet rank was applied
*or* when it was absent/stale (`reason: "fleet rank absent — static order"`),
so the retrospective can see which manifests were fleet-informed. Candidates
= targets after region policy, with `filtered: region not permitted` rows for
the ones policy removed. `parent_decision_id` = the fleet rank's id.

### 4.4 Client picker (benchday `MeshRoutePicker` / `AsrService`)

Today `asr.picker` emits `probe_cycle` and `quarantined` telemetry and the
flight recorder logs `input.asr.start{serverEndpoint}` and
`input.asr.final{serverUsed}`. Add a `pick` record at `pickBest()` time with
the picker's full `ranked()` output as candidates — `MeshRouteObservation`
already carries `score`, `rttMs`, `taskEwmaMs`, `state`, quarantine and
failure counts, so the rows are nearly free; add the load report and
`remeasure` flags. Emit `failover` when `pickBest(exclude:)` runs. These ride
the existing telemetry channel to the hub (bounded, see §6), tagged with
`parent_decision_id` from the manifest.

This is the emitter that answers the operator's example directly: the pick
record shows the phone chose tower0 (because its EWMA said 300 ms) while
xc-tower-ubuntu's row says `load in_flight=0, rtt 40 ms, outcome: ranked
lower — EWMA unmeasured, bootstrapped to leader's score`. A reader sees the
bootstrap rule made the call, and can decide whether that rule is right.

### 4.5 Job caller (media-corpus, and any `/fleet/admit` consumer)

Emits the outcome for its admit decision, plus a `failover` record with
`reason: fleet_unavailable` when it fell back.

## 5. The retrospective — turning records into "should have gone to X"

A pure function, `retro.py` in `livestack_node` (no I/O; takes an iterable
of records). For each decision with an outcome it computes:

1. **Counterfactual rank.** Re-run the *same* ranking rules over the recorded
   candidate rows (the values are in the record, so this needs no live
   system) — and re-run with each rule relaxed one at a time: ignore
   distance; ignore load; ignore membership filter; treat *no opinion* load
   as idle. Any relaxation that changes the winner is a **sensitivity**: "this
   decision hinged on load; if load had been unmeasured the answer flips."
2. **Idle-alternative check** — the operator's example, made mechanical. A
   candidate is an *idle alternative* if it was fresh+ready, `in_flight == 0`,
   `pressure` below the chosen one's, and its distance band was ≤ the
   chosen one's. If one exists, the record is flagged
   `missed_idle_alternative` with that candidate named and the rule that
   ranked it lower quoted.
3. **Outcome vs. expectation.** `latency_ms` against the candidate's
   `taskEwmaMs`/band; a chosen candidate that took 3× its expected latency is
   `underperformed`, and if an idle alternative existed the flag becomes
   `misrouted` — the strongest finding, and the one a future agent should
   read first.
4. **Staleness.** Decisions whose winning input was older than `ttl_s` are
   `decided_on_stale_inputs`.

Output is a list of findings, each `{decision_id, finding, detail,
alternative, rule}`; the `rule` names the code path (`"bootstrap:
unmeasured node scored to leader"`, `"band: >=600 vs <50"`,
`"membership: state=suspect"`). That name is what a future agent greps for
in the code to change behaviour.

**Aggregation** (a second pure function): findings grouped by `rule` and by
`(chosen host, alternative host)` pair, with counts and median latency
delta. "In 41% of NA picks in the last 24 h, `bootstrap` kept traffic on the
mac while xc-tower-ubuntu sat idle" is a sentence that should fall out of
this with no further analysis. That sentence is the improvement backlog.

**Surface:** `GET /fleet/retro?since=…&kind=…` on the fleet broker over its
own ledger, and a `benchday` CLI subcommand (`benchday fleet retro`) that
pulls the hub's records and the fleet broker's and joins them. Both print
the aggregation first and the individual findings second.

## 6. Storage — bounded, by rule 10, before the first record is written

Every ledger is a store that grows with traffic, so each declares its bound
and its enforcer up front, and each gets a row in benchday's
`docs/daemon-storage-bounds.md` inventory. **The precedent to avoid:**
`~/.benchday/llm-costs.jsonl` is an append-only JSONL with no rotation and
no inventory row — it is the shape this design must not add another of.

| ledger | where | format | bound | enforcer |
|---|---|---|---|---|
| host broker | `~/.cache/livestack/decisions-<host>.jsonl` | JSONL | **rotate at 32 MiB × 4 files** and **prune > 14 d** | the writer (size check on append, copy-truncate), same shape as benchday's `logging.rs` rotating writer |
| fleet broker | `~/.cache/livestack/fleet-decisions.jsonl` | JSONL | **64 MiB × 4**, **30 d** | same writer |
| hub manifest + client picker | Postgres `activity_events` (`source = "route.decision"` / `"route.outcome"`, record in `ctx` JSONB, `trace_id = decision_id`) | existing table | **30 d** | existing `pruneActivity` in `retention-sweeper.ts` — already bounded, already inventoried |
| job caller | its own log; media-corpus writes to its run log | append | its existing bound | its existing rotation |

Rate bounds, because a per-tick emitter is how a 92,089-line log happened:
the host broker emits only when a plan has actions; the fleet broker emits
`observe` only on membership *transitions*; the client emits one `pick` per
`pickBest` (not per `ranked`/snapshot call — `ranked()` is also called for
diagnostics and would multiply records). A record is capped at 32 KiB; a
candidate list over 64 entries is truncated with `"truncated": n`.

Unset retention must mean **disabled**, never "delete on deploy" — the rule
the retention sweeper already follows.

## 7. Tests the implementer must write

- Schema: a record from each emitter validates against one JSON schema
  (`decision.schema.json` in `node-py/livestack_node/`), and the hub's
  TypeScript type is generated from it, not hand-copied.
- Completeness: for a rank over N candidates the record has exactly N rows,
  each with `outcome ∈ {chosen, ranked, filtered}` and a non-empty `reason`;
  filtered rows precede ranked rows.
- Correlation: an end-to-end test (benchday isolated e2e, which has a real
  hub + real daemons) that makes one dictation through a manifest with
  `fleet_rank` and asserts the hub has a `route.decision` and a
  `route.outcome` with the same `trace_id`, and that the `pick` record's
  `parent_decision_id` equals the manifest's.
- Retro: pure tests over hand-built records — the operator's scenario
  (chosen remote+busy, alternative idle+near) yields `misrouted` naming the
  alternative and `rule = "bootstrap: …"`; a record where the alternative
  was `filtered: state=suspect` yields **no** finding (the decision was
  right given what was known); a relaxation flip yields a `sensitivity`.
- Bounds: the JSONL writer rotates at the configured size and prunes by age
  in a temp dir; the hub rows are deleted by the existing sweeper test with
  `source = "route.decision"`.

## 8. What this is not

- Not the flight recorder. That captures the *session* for replay; this
  captures *decisions* for audit. They share `decision_id` where they touch
  (`input.asr.start` should carry it) and nothing else.
- Not metrics. Counts and percentiles fall out of the retrospective; the
  ledger stores the reasoning, which no metric can reconstruct.
- Not a place for content. No audio, no text, no prompts.

## 9. Handoff — task list

- [x] `decision.schema.json` + Python dataclass + generated TS type — schema and
      dataclass shipped (`node-py/livestack_node/decision.schema.json`,
      `ledger.py`). The hub's TS type is hand-written in `route_decision.ts`
      rather than GENERATED from the schema; generating it is still open
- [x] rotating JSONL writer in `livestack_node` (size × files, age prune, 32 KiB record cap); tests
- [x] host-broker emitter on `plan_and_apply`; `Grant` gains a reason; tests
- [x] fleet-broker emitters: `observe` on transitions (P1), `rank` (P2), `admit` (P3)
- [x] hub manifest emitter → `activity_events`; `fleet_rank.decision_id` in the manifest
- [ ] client `pick` / `failover` records via telemetry; `parent_decision_id`; `X-Livestack-Decision` header + ws `decision=` param; `input.asr.start` carries the id
- [ ] node-side outcome event on request completion (facade middleware reads the header)
- [ ] `retro.py` pure functions + `GET /fleet/retro` + `benchday fleet retro`
- [x] storage-bounds inventory rows for both JSONL ledgers — plus the hub's
      `activity_events` `route.decision` rows and the in-memory `FleetRankRegistry`,
      in benchday `docs/daemon-storage-bounds.md`
- [ ] isolated-e2e correlation test — the manifest half is asserted
      (`fleet_rank` reaches the manifest, and a ranking cannot widen it); the
      JOIN across emitters is not, because §4.4's client emitter does not exist yet

### What shipped, and the one field the design gained

Emitters live: **host broker** (one record per Evict/Load/Grant/Defer a plan
produced, with the candidate rows the log line never had), **fleet broker**
(`observe` per membership transition, `rank` per `/fleet/rank`, `admit` per
`/fleet/admit`), **hub manifest** (one `route.decision` row per
`/v1/speech/routes`, region-filtered targets present as `filtered` rows,
`parent_decision_id` naming the fleet rank).

Two things the implementation added that §2 did not have, both because a live
ledger showed them missing:

* **`decision_id` is a MONOTONIC ULID.** A plain ULID orders by its random
  component inside a millisecond, so a burst sorts arbitrarily — which the
  retrospective, replaying a ledger in write order, would have hit immediately.
* **`dispatched`.** An observe-only broker computes plans constantly and
  dispatches none of them, so its `evict` records were indistinguishable from a
  host broker's and a reader would have counted evictions that never happened.
  In the one artifact whose entire value is being trustable about what happened.

### What is left, and what it is blocked on

The three open items are one story: **nothing yet records an OUTCOME**, so every
record answers four of §1's five questions and none answers the fifth. The
client `pick` emitter (§4.4) is the keystone — it is where `parent_decision_id`
gets used, where `X-Livestack-Decision` starts its journey, and what `retro.py`
would have anything to join. `retro.py` written before it would have only
half-records to reason over, which is why it is last rather than first.
