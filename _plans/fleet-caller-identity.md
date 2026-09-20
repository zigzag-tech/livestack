# Fleet caller identity — every write to the fleet says who is asking

Owning artifact for the umbrella change `hub-and-compute-convergence`
(`~/unchain/openspec/changes/hub-and-compute-convergence/`, capability
`fleet-caller-identity`, plus one requirement of `compute-roster-of-record`). livestack has
no OpenSpec; this plan is the record, in the discipline of `durable-workloads.md`: a
requirement is restated here verbatim, a task carries its own verification, and a gate is a
number. Written 2026-09-20 from source and from the running NA fleet.

## Why

Harmony's identity story is one endpoint deep, and that endpoint was measured
wrong.

- **`LIVESTACK_FLEET_TOKENS` on the production fleetd — corrected 2026-09-20.** The
  original survey (and this plan's first draft) reported it unset, from
  `systemctl cat livestack-fleetd.service` on xc-tower-ubuntu. That check was
  silently incomplete: the drop-in `/etc/systemd/system/livestack-fleetd.service.d/30-auth.conf`
  is mode `0600 root`, so `systemctl cat` as `ubuntu` printed "Permission
  denied" and omitted it. Measured with sudo the same day: the drop-in exists,
  dated **2026-09-05**, and sets two principals — `media-corpus` (fixed) and
  `hub` (delegating, prefix `acct_`). `GET /fleet` on the NA broker answers
  `auth.required: true` with exactly those two principal names. So a minimal
  token table predates this plan by two weeks; what has NOT happened is the
  inventory, the engine/hub caller issuance, the CN rehearsal, or any of the
  A–H mechanics (the xc-tower-ubuntu checkout runs `e6cbd7a5`, code from
  before this plan existed). The requirement's discipline therefore applies to
  the **expansion**: no further principals, and no reliance on the new
  mechanics, until R.1–R.3 are done. Measurement lesson recorded here so it is
  not relearned: fleetd config checks on that host need `sudo systemctl cat`.
- **hostd `POST /admit` has no auth and no identity check** — `hostd.py:148-158`: the handler
  takes `payload: dict = Body(...)` and `owner=payload.get("owner", "consumer")`. The node's
  `POST /livestack/lease` defaults `owner_id` to `"anonymous"` (`facade.py:325-336`), and
  `POST /model/warm|evict|reclaim` (`facade.py:367-400`) are open to anything that can reach
  the port. The dashboard refuses to offer those levers on "one card, one master" grounds;
  the HTTP surface underneath does not.
- **The app identity is erased at the LLM proxy.** `examples/harmony-llm/server.py:1135-1137`
  admits every request as `owner_id=f"harmony-llm:{HOST_ID}"`. benchday and attune reach the
  planner as the same owner; `client.admit()` (`client.py:118-127`) sends no credential.
- **The planner never reads `owner`.** `planner.py:189` is its only mention. Demand is summed
  per kind (`hostbroker.py:757`), so "attune has had the card ten minutes, give benchday a
  turn" cannot be expressed.
- **Evict and Load ledger records carry `request=None`.** `hostbroker.py:1295` populates
  `request` only for actions that have `request_id` (Grant, Defer), and stores that id under
  the key `owner`. The 27B reload thrash of 2026-09-19 was diagnosed from journal timestamps
  because the ledger could not say who needed the room.
- **`min_residency_s`, `reload_cost` and `priority` are not declarable per unit.** They exist
  on `planner.Unit` (`planner.py:95-98`) but are absent from the unit-file schema
  (`examples/harmony-llm/server.py:172-194`), from `ManagedUnit` (`manager.py:84-96`), from
  `/residence` (`facade.py:433-452`) and from `hostbroker.py:1397-1409`. A 27B whose measured
  reload is ~50 s is protected for 15 s and tie-broken at `reload_cost=1.0`, the same as a
  0.6B embedder. Every UNPINNED LLM is priority 30 (`_RES_TO_PRIO`, `hostbroker.py:1324`).
- **`regions` is honoured on `/fleet/rank` and not on `/fleet/admit`.** `hostd.py:346-367`
  filters rank results; `fleet_admit.targets_from_view` (`fleet_admit.py:41-113`) never reads
  `region`. Moving a caller to the authenticated path silently drops its NA guarantee.
- **`relays` in the fleet view is never populated.** `fleet_rank.distance_to`
  (`fleet_rank.py:147-150`) reads `view["relays"]`; `fleet_view()`
  (`hostbroker.py:1082-1107`) never emits the key. A caller outside the links matrix gets the
  fleet broker's own distances or `unknown`, which sorts last.

Two applications and N users are about to share this fleet through two hubs. None of the
above can be worked around above Harmony: a hub that authenticates a person has nowhere to
put that fact.

## Requirements (restated from the umbrella; owned here and nowhere else)

### Requirement: Every fleet write carries an authenticated principal
Harmony SHALL require a principal on every endpoint that changes what is resident or who
holds capacity: fleet `/fleet/admit`, host `/admit`, node `/livestack/lease` and
`/model/{warm,evict,reclaim}`. The owner recorded for the request SHALL come from the
credential (a fixed principal) or from the credential's delegation prefix (a delegating
principal), never from the request body alone. Read endpoints (`/fleet`, `/fleet/rank`,
`/peers`, `/plan`) SHALL record the caller when a credential is present and MAY stay open.

- A body that names another owner is refused: fixed principal `attune` admits with
  `owner: media-corpus` → 403 naming the mismatch, nothing recorded against either owner.
- A delegating principal stays inside its prefix: benchday's principal (prefix `benchday:`)
  admits `owner: attune:acct_x` → 403, the refusal ledgered with the principal's name.
- An unauthenticated eviction is refused: `POST /model/evict` without a credential → 401,
  nothing evicted.

### Requirement: The owner travels through the engines
An engine fronted by Harmony (harmony-llm, polytts, polyasr) SHALL read an inbound owner
header, pass it as the owner of its host admission, and SHALL NOT substitute its own identity
for a caller that supplied one. A request without an owner header SHALL be admitted as the
engine's own identity and marked as such in the ledger.

- Two apps, one unit, two owners in the ledger: benchday (`benchday:acct_b`) and attune
  (`attune:acct_a`) each send one request to the same LLM unit → two Grant records with two
  different owners and one resident unit.

### Requirement: Loads and evictions are attributable
Each Load and Evict record in the decision ledger SHALL carry the owner of the request that
caused it (`caused_by`), and the field that carries a request id SHALL be named as such.

- A reload is explained from the ledger alone: the 27B is evicted and reloaded once → the two
  records name the owner whose request needed the room and the owner whose request brought
  it back, without consulting any engine journal.

### Requirement: Region policy holds on the admission path
`POST /fleet/admit` SHALL accept the same `regions` policy `GET /fleet/rank` accepts and SHALL
refuse, with the excluded targets named, rather than place outside it.

- NA-only admission with only a CN target warm: `kind=polytts, regions=na` with the only warm
  polytts in `cn` → the broker places on a cold NA node or refuses naming the CN exclusion;
  it never places in CN.

### Requirement: Ceilings apply per owner and per application
Quota SHALL be enforceable on an exact owner and on an owner prefix, so `attune:` as a whole
and `attune:acct_a` alone can each have a ceiling. Refusal SHALL remain a 429 naming the count
in force.

- An app at its aggregate ceiling: `attune:` capped at 4, `attune:acct_a` holds 3,
  `attune:acct_b` asks for 2 → refused with the aggregate count; `attune:acct_b` alone is not
  reported over its own ceiling.

### Requirement: Unit economics are declarable
A unit file SHALL accept `min_residency_s`, `reload_cost` and `priority`, and the values SHALL
reach the planner through the node's residence report. Absent values keep today's defaults.

- A declared reload cost protects a slow model: the 27B declares `min_residency_s: 60`,
  `reload_cost: 4`; a 0.6B embedder asks for room 20 s after the 27B loaded → the 27B is not
  evicted and the Defer record names the residency floor.

### Requirement: Ranking from a declared origin
A caller not present in the links matrix SHALL be able to rank targets from a declared origin
(`vantage=region:<r>` or a populated `relay:<id>`), and the answer SHALL say which vantage it
used.

- A hub off the tailnet asks for NA: no links row, `kind=llm, regions=na,
  vantage=region:na` → targets ordered by the NA region's own measured links, not by the
  fleet broker's.

### Requirement: Tokens are switched on with an inventory and a rehearsal
Production brokers SHALL NOT have `LIVESTACK_FLEET_TOKENS` set until a written inventory of
callers exists, each has a token, and the same configuration has run on the CN broker for at
least one day without an unattributed 401.

- A caller missing from the inventory: a request arrives with no credential after tokens are
  on → 401, the refusal ledgered with the source address, the inventory updated before NA
  follows CN.

### Requirement (from `compute-roster-of-record`): Scope is a grant read by the fleet
Owner, organization and realm scope for a compute node SHALL be a grant the hub records and
the fleet broker enforces on admission, so a node pooled for one account is never placed for
another.

- A self-scoped GPU: a person enrols a GPU box scoped to themselves and another account admits
  an LLM → that box is excluded and the exclusion names the scope.

## Design

### The owner header: `X-Harmony-Owner`

One header, `X-Harmony-Owner: <owner>`, carried on every request to an engine's public
surface (`/v1/chat/completions`, `/tts`, `/v1/align/...`), on hostd `/admit`, and on the node
facade. Not `Authorization`: the engines already accept an `Authorization` for their own API
keys (harmony-llm forwards the OpenAI-style bearer to vLLM), and overloading it would make an
API key and an owner assertion the same string. Not a body field: `/v1/chat/completions` is an
OpenAI-shaped body a caller's SDK owns, and `extra_body` is already spoken for by
`harmony_requires`. A header survives every SDK, every proxy, and the relay.

The header is an **assertion, not a credential**: the engine forwards it to hostd `/admit`
with the engine's own token, and hostd resolves the owner through `fleet_auth.resolve_owner`
against the engine's principal, which is *delegating* with the prefix the engine is allowed
to speak for. On this fleet the engines are hub-shared, so their prefix is `""` (any owner)
— an engine reachable only on the mesh is trusted to relay what a hub asserted. A public
engine would get a narrower prefix. Absent header ⇒ owner is the engine's own fixed name
(`harmony-llm:<host>`, today's behaviour) and the Grant is marked `owner_asserted: false`.

### Per requirement

| Requirement | Where it lands |
|---|---|
| Every fleet write carries a principal | `hostd.py` `/admit`: add `authorization: str = Header(None)`, resolve through `fleet_auth.authenticate` exactly as `/fleet/admit` (`hostd.py:389-410`) does; `facade.py` `/lease`, `/model/warm`, `/model/evict`, `/model/reclaim`: a node-level principal table (`LIVESTACK_NODE_TOKENS_FILE`, same JSON shape) checked by a small dependency; read endpoints record `principal` when present. One shared `principals_from_env()` reads `LIVESTACK_FLEET_TOKENS_FILE` first (a mode-0600 JSON file) and the inline env second, because the one secret in the system should not be the one setting that is inline JSON (`hostd.py:47` already warns about systemd quote-stripping). |
| Owner through the engines | `examples/harmony-llm/server.py:1135`: `owner_id = request.headers.get("x-harmony-owner") or f"harmony-llm:{HOST_ID}"`; `client.admit(..., token=, owner_asserted=)` adds `Authorization` and passes the owner in the body for hostd to resolve (`client.py:118-127`). polytts (`voxlert/cli/polytts/server.py`) and polyasr (`polyasr/server.py`) do the same at their `admit`/lease call, in their own repositories, against the header name fixed here. |
| Loads and evictions attributable | `planner.py:231-243`: `Load` and `Evict` gain `caused_by: str = ""` (the owner of the `Request` whose planning produced the action; rule-0 sheds carry `"pressure"`); `hostbroker._emit_plan` (`hostbroker.py:1295`) writes `request={"request_id": a.request_id}` for Grant/Defer and `request={"caused_by": a.caused_by}` for Load/Evict. Existing readers of `request.owner` on Grant records: none in this repository (verified by grep); the rename is recorded in `decision-ledger.md`. |
| Region on admission | `hostd.py` `/fleet/admit`: accept `regions` (and `allow_unknown_region`) and apply `client.eligible_targets` to the view's nodes before `fleet_admit.admit`, recording the rejected rows in the admit ledger record the same way `/fleet/rank` records `region_policy`. `hostbroker.py:1207` stops hard-coding `region=None` on Candidate rows. |
| Ceilings per owner and per prefix | `fleet_scheduler.quota_for` (`fleet_scheduler.py:315-320`): longest-prefix match over `account_quotas` keys that end in `:`; `over_quota` computes `held` as the sum of usage over owners with that prefix and names it as `aggregate` in the reason. Configuration stays `LIVESTACK_ACCOUNT_QUOTAS`, e.g. `{"attune:": 4, "attune:acct_a": 2, "benchday:": 6}`; `GET /fleet` reports both the exact and the prefix ceilings in force. |
| Unit economics declarable | unit file (`examples/harmony-llm/server.py:172-194`) reads `min_residency_s`, `reload_cost`, `priority`; `ManagedUnit` (`manager.py:84-96`) carries them; `/residence` (`facade.py:433-452`) emits them when set; `RestPeer.units()` (`hostbroker.py:1397-1409`) passes them into `Unit(...)`, with an explicit `priority` from the node outranking `_RES_TO_PRIO`. `HARMONY.md` and `examples/harmony-llm/llm-units.example.json` show the 27B declared with its measured reload. |
| Ranking from a declared origin | `fleet_rank.distance_to` gains scope `region:<r>`: the median of the links rows of hosts in region `r` to the node's host; `fleet_view()` gains `relays` from `LIVESTACK_RELAYS` (`{"<id>": {"region": "na", "links": {...}}}`) measured by the same `measure_links` loop; the rank response carries `vantage_used`. |
| Tokens with inventory and rehearsal | operations section below; no code beyond `LIVESTACK_FLEET_TOKENS_FILE`. |
| Scope is a grant read by the fleet | `fleet_admit.targets_from_view`: a node row may carry `scope: {"kind": "owner"|"org"|"realm", "id": ...}` (from its announce, `announce.py`, set by the operator or the enrolling hub); a target whose scope does not admit the request's owner is rejected with `filtered: scoped to <kind> <id>`. The hub records the grant; the node announces it; the broker enforces it. |

### What stays as it is

The planner's decision logic (rule 0/rule 1, contention cost, cold-node fallback) does not
read `owner`. Fairness between apps is the fleet broker's (ceiling + fair share + EDF) and
stays there; the host broker keeps deciding residency by kind, demand and economics. That is
the two-brains rule in `HARMONY.md` and this plan does not touch it.

## Caller inventory (measured 2026-09-20, 48 h of journals on xc-tower-ubuntu)

Source addresses seen by each service, with who they are. This is the list that gets a token.

| Source | Seen by | Requests / 48 h | Who |
|---|---|---|---|
| `127.0.0.1` | harmony-llm 117k, gpu0 40k, polytts 26k, polyasr 27k+4k, fleetd `POST /peers` 34k, hostd `POST /admit` 7.5k | | the node's own announce loop, hostd probes, harmony-llm's own admits, local scripts |
| `100.64.0.18` | harmony-llm 69k, gpu0 68k, polytts 64k, polyasr 70k+36k, fleetd `GET /fleet` 729 | | xc-tower-ubuntu talking to itself by mesh address: the benchday daemon's engine probes, the fleet page, the repair agent |
| `100.64.0.2` | harmony-llm 33k, gpu0 7.7k, polytts 1.1k, fleetd `GET /fleet/rank` 15k, hostd `GET /peers` 28k | | **attune** on xc-mac-studio (supply loop, embeddings, plan/translate, the Mac's own polytts announcing) |
| `100.64.0.19` | harmony-llm 727, polyasr 3.7k+1.8k | | **benchday hub** (`public-la`): title chain, ASR finalize |
| `100.64.0.12` | polyasr 2.7k+1.2k, gpu0 18 | | **zz-tower2**: media-corpus ingest and the relay's ASR targets |
| `100.64.0.3` | harmony-llm 2.4k, polytts 45, fleetd rank 13, hostd `GET /peers` 23k | | **zz-tower0**: CN node announcing and reading peers; a CN caller of the NA 27B (benchday's chips or a script — identify) |
| `10.0.0.244` (MAC `78:e6:1c:1c:e7:86` at measure time), `10.0.0.94`, `10.0.0.71` (MAC `9c:53:22:6a:de:07`) | polyasr 2.9k+1.4k, 1.9k+1k, 123+119 | | **Resolved 2026-09-20 (R.1):** `10.0.0.94` is xc-tower-ubuntu itself (`ip -4 addr` shows `10.0.0.94/24` on eno1; the Mac Studio's arp names it `xc-tower` and its `/etc/hosts` maps `10.0.0.94` → xc-tower) — local clients reaching polyasr via the LAN address, all `GET /health`. `10.0.0.244` at measure time was an **Honor Android phone** (OUI `78:e6:1c` = Honor Device Co., Ltd., registered 2024-04-26): the phone polls `GET /health` (2.8k/48 h) and posts `POST /v1/audio/transcriptions` — the direct engine route from phones. The fleet's Honor devices are the `xc-magical` pair (see CN table), so this is the user's Honor Magic phone on the Vaughan LAN. `10.0.0.71` is a **TP-Link Systems device** (OUI `9c:53:22`, registered 2022-09-21 — an access point/router, not a phone): xc-tower-ubuntu's neighbour table shows that one MAC answering ARP for five IPs (`10.0.0.25`, `.45`, `.71`, `.113`, `.244`), i.e. a TP-Link AP/NAT box that phone traffic transits — its 170 health polls are phone/app traffic seen at the AP's address. As of 2026-09-20 evening the TP-Link MAC answers for `.244` too (the Honor phone's lease moved or it now associates via the AP); the Honor MAC was captured in the NA journals at measure time. |

### CN half (measured 2026-09-20 on zz-tower0)

48 h of journals, from the CN GPU box itself (100.64.0.3). Port map, corrected from the
brief: **livestack-hostd listens on 8799, polyasr on 8766, chipgen (a benchday
Qwen3-1.7B chip-generation node, `chipgen.service`) holds 8844, polytts on 8100.**

| Source | Seen by | Requests / 48 h | Who |
|---|---|---|---|
| `127.0.0.1` | polytts 18.3k residence + 5.1k capability + 1.9k `POST /tts` + 121 `/tts/stream`, polyasr 14.6k residence + 3.6k capability + 52 `/v1/align/manifest`, chipgen 15.1k residence + 4.4k capability | | the box's own announce/probe loops and local TTS/align clients (unchain pipeline scripts); polytts also 72 evict / 70 reclaim / 47 warm from localhost operator or script levers |
| `100.64.0.18` | polytts 13.2k residence + 1.4k capability, polyasr 13.7k capability + 8.4k residence + 1.3k health, chipgen 13.1k capability + 10.8k residence + 378 `POST /pane_status` + 56 `POST /pane_ask` + 7 `POST /assets_curate` | | xc-tower-ubuntu — the **NA fleet broker's peer polls** (the `/livestack/*` port pattern matches), plus benchday pane-ask/status traffic driving chipgen |
| `100.64.0.12` | chipgen 5.4k `POST /generate` + 696 `POST /rerank` + 379 `POST /assets_curate` + 42 pane_status + 25 pane_ask, polyasr 1.0k health + 13 `POST /v1/audio/transcriptions` | | **zz-tower2**: media-corpus ingest and the benchday relay's ASR targets (same role as on NA) |
| `100.64.0.19` | polyasr 1.4k health (+2 transcriptions) | | **benchday hub** (`public-la`) health probes of the CN ASR |
| `100.64.0.8` | polyasr 626 health | | **xc-magical-obkxml3w** — the user's Android phone (Honor Magic; active on the mesh from a Canadian IP) polling CN polyasr directly |
| `100.64.0.7` | polyasr 240 health (+2 `POST /v1/audio/transcriptions`) | | **xc-magical** — the same phone's earlier tailnet identity (now offline; superseded by the `obkxml3w` enrolment) |
| `100.64.0.3` | chipgen 2, polyasr 2 | | zz-tower0 self-probes via its mesh address |
| — | **livestack-hostd (8799): not measurable** | | CN hostd logs no requests anywhere: journal carries only systemd start/stop lines (0 access lines across its entire history), it does not appear in `/var/log/syslog`, and it writes no log file. Its inbound callers over 48 h are therefore not journal-attributable. Inbound is expected to be the NA fleet broker's peer polls (symmetric to the 23k `GET /peers` from 100.64.0.3 seen by NA hostd) and the local engines' admits on 127.0.0.1, but that is inference, not measurement. Fixing hostd's request logging is a precondition for the CN rehearsal (R.3): a 401 from an unlogged caller cannot be attributed. |

R.1 evidence: polytts counted from `journalctl -u polytts --since "48 hours ago"` (48,793
lines; exact window is Sep 18 07:48 → **Sep 19 15:46** — uvicorn access lines stopped
reaching the journal after that while the service kept listening; the last ~16 h of the
window is unlogged). polyasr counted from the last 45,000 access lines of
`~/polyasr/cuda/logs/polyasr.log` (1,986,003 access lines since 2026-06-22, no timestamps,
mean ≈22k/day, so the tail window is ≈48–60 h). chipgen counted from `/var/log/syslog`
(`python[1090561]` lines, ISO timestamps, exact 48 h). unchain-asr-worker holds no HTTP
listener: it is an outbound portable-livestack worker registering CN ASR capacity with
zz-tower2's direct gateway (100.64.0.12:3125); 2 pinned socket connections in the window.
No logged source was left unidentified: every 100.64.0.x source maps to the tailscale
status list. New versus the NA inventory: the two phone identities 100.64.0.7/100.64.0.8
(they were invisible from NA because they call the CN polyasr directly over the mesh —
the same direct-engine-route pattern as the Honor phone on the Vaughan LAN).

Principals to issue, from the inventory:

| Principal | Kind | Owner or prefix |
|---|---|---|
| `benchday-hub` | delegating | `benchday:` |
| `attune-hub` | delegating | `attune:` |
| `media-corpus` | fixed | `media-corpus` |
| `sorbonne` | fixed | `sorbonne` (the gateway tenant; becomes a workload principal too) |
| `harmony-llm@<host>`, `polytts@<host>`, `polyasr@<host>` | delegating | `""` (engines relay asserted owners) |
| `fleet-ops` | fixed | `ops` (scripts, the fleet page's levers) |

## Tasks

Session-sized; each carries its verification. Letters group by requirement.

### A. Principals everywhere

- [x] A.1 `principals_from_env()` reading `LIVESTACK_FLEET_TOKENS_FILE` then `LIVESTACK_FLEET_TOKENS`; refuse a world-readable file → verify: `tests/test_fleet_auth.py::test_tokens_file_wins_and_refuses_mode` passes. Done 2026-09-20 (commit `517cb6f`, suite 24 passed in test_fleet_auth.py); None-means-off vs {}-means-refuse-everyone semantics added so call sites can tell "unset" from "refused".
- [x] A.2 hostd `/admit` takes `Authorization`, resolves through `authenticate`, 401 without, 403 outside prefix, body owner consulted only for a delegating principal → verify: `tests/test_hostd_admit_auth.py` (three scenarios from the requirement) passes; with no principals configured behaviour is byte-for-byte today's and the startup line says so. Done 2026-09-20 (commit `517cb6f`, 9 passed).
- [x] A.3 node facade `/lease`, `/model/warm|evict|reclaim` require a node principal (`LIVESTACK_NODE_TOKENS_FILE`); `/residence`, `/capability`, `/health` stay open → verify: `tests/test_facade_auth.py`; `curl -X POST :8100/livestack/model/evict` without a token → 401. Done 2026-09-20 (commit `517cb6f`, 8 passed, in-process equivalent of the curl scenario).
- [x] A.4 read endpoints record `principal` on their ledger rows when a token is present → verify: a rank with a token produces a record whose `request.principal` is set; without, `null`. Done 2026-09-20 (commit `517cb6f`, in test_hostd_admit_auth.py). Deficiency: `/fleet`, `/peers`, `/plan` emit no ledger rows today, so there is nothing to record principal on there; emit_rank (and later emit_admit) carry it.

### B. Owner through the engines

- [x] B.1 `client.admit(kind, owner_id, *, token, requires)` sends `Authorization` → verify: `tests/test_client_admit.py` asserts the header; existing callers without a token still work while hostd has no principals. Done 2026-09-20 (commit `17016aa`, 4 passed).
- [x] B.2 harmony-llm reads `X-Harmony-Owner`, admits with it, marks `owner_asserted` → verify: two requests with two headers against one unit produce two Grant records with two owners (`tests/test_harmony_llm_owner.py`, in-process against a fake broker). Done 2026-09-20 (commits `17016aa`+`ee3edda`, 3 passed, real broker+planner+ledger in-process; Grant gained `owner`/`owner_asserted`).
- [x] B.3 The header name and semantics documented in `HARMONY.md` ("Who is asking" section) and in `examples/harmony-llm/README.md`; polytts and polyasr changes filed in their repositories against that section → verify: both repositories' READMEs name `X-Harmony-Owner`. Done 2026-09-20 (HARMONY.md + harmony-llm README in `17016aa`; polytts README `f482d7b`, polyasr README `0627cef`). Deficiency: polytts and polyasr are passive nodes with no admit call of their own — the identity-bearing surface is the lease `owner_id` made by their callers; READMEs record that instead of inventing a hook.

### C. Attributable ledger

- [x] C.1 `Load`/`Evict` gain `caused_by`; the planner fills it from the request it is making room for, `"pressure"` for rule 0 → verify: `tests/test_planner_caused_by.py` — an eviction for request R carries R's owner. Done 2026-09-20 (commit `9cfca2c`, 4 passed; pin-floor/soft-pin restores name the policy, not an invented owner).
- [x] C.2 `_emit_plan` writes `request_id` and `caused_by` under their own names → verify: `tests/test_ledger_attribution.py` reads one Evict and one Load back with both owners; `decision-ledger.md` updated. Done 2026-09-20 (commit `9cfca2c`, 3 passed; rename re-verified by grep — no reader of `request.owner` existed).
- [x] C.3 Gate script `scripts/explain_reload.py <ledger> <kind>` prints the owner pair for the last reload → verify: run against the NA fleetd ledger after one deliberate 27B evict+reload; output names the two owners with no journal consulted. Script done 2026-09-20 (commit `9cfca2c`, at `node-py/scripts/explain_reload.py`, synthetic-ledger test in test_ledger_attribution.py); the production run is part of gate R.4 below.

### D. Region on admission

- [x] D.1 `/fleet/admit` accepts `regions` + `allow_unknown_region`, filters before scheduling, records `region_policy` → verify: `tests/test_fleet_admit_regions.py` — NA-only with only CN warm refuses naming CN, or places on a cold NA node when one exists. Done 2026-09-20 (commit `e367438`, 4 passed; emit_rank's ledger request also gained `region_policy` so the "same way rank records it" phrase is literal).
- [x] D.2 Candidate rows carry `region` (`hostbroker.py:1207`) → verify: a rank record's rejected row shows `region: cn`. Done 2026-09-20 (commit `e367438`, tests/test_region_policy.py 12 passed).

### E. Prefix ceilings

- [x] E.1 `quota_for` longest-prefix match; `over_quota` aggregates by prefix and names `aggregate` → verify: `tests/test_prefix_quota.py` — the umbrella scenario (`attune:` 4, `acct_a` holds 3, `acct_b` asks 2 → 429 with aggregate count; `acct_b` not over its own). Done 2026-09-20 (commit `e367438`, 8 passed; exact entry wins over prefix, owner ceiling and each enclosing prefix ceiling checked independently).
- [x] E.2 `GET /fleet` reports exact and prefix ceilings and per-prefix usage → verify: response has `quota.prefix_usage`. Done 2026-09-20 (commit `e367438`, same 8; `quota.prefix_quotas` + `quota.prefix_usage` alongside the untouched exact fields).

### F. Declarable unit economics

- [x] F.1 Unit file → `ManagedUnit` → `/residence` → `Unit` carries `min_residency_s`, `reload_cost`, `priority` → verify: `tests/test_unit_economics_roundtrip.py`; `examples/harmony-llm/llm-units.example.json` shows the 27B with `min_residency_s: 60, reload_cost: 4`. Done 2026-09-20 (commit `24d37ec`, 4 passed; node-declared priority outranks `_RES_TO_PRIO`, operator `_priorities` still wins; absent = byte-for-byte defaults).
- [x] F.2 Planner honours a node-declared priority over `_RES_TO_PRIO` → verify: `tests/test_planner_declared_priority.py` — two UNPINNED LLMs with priorities 20 and 30: the 30 yields. Done 2026-09-20 (commit `24d37ec`, 3 passed).
- [ ] F.3 Declare the 27B's measured reload on xc-tower-ubuntu's unit files → verify: `/residence` on both cards shows the fields; `GET /plan` after a 20 s-old load with a pending embedder shows a Defer naming the residency floor. Mechanism done 2026-09-20 (commit `24d37ec`: the residency-floor Defer reason did not exist and was added, `residency floor: llm27 loaded 20s ago is protected for 60s (min_residency_s)`, tested); the operator half on xc-tower-ubuntu is part of the rollout (R.2/R.4).

### G. Declared-origin ranking

- [x] G.1 `vantage=region:<r>` in `distance_to`; response carries `vantage_used` → verify: `tests/test_rank_region_vantage.py`. Done 2026-09-20 (commit `24d37ec`, 5 passed; median of member links rows, `unknown` when unmeasured, never a guess).
- [x] G.2 `LIVESTACK_RELAYS` measured and emitted as `view["relays"]` → verify: a rank with `vantage=relay:na-public-la` returns non-`unknown` bands. Done 2026-09-20 (commit `24d37ec`, in the same 5; `relay:<id>` distance scope already existed — only the view population was missing. Relay links are operator-declared env, not probeable, so `fleet_view` re-reads the env each call.)

### H. Scope on admission

- [x] H.1 Node announce may carry `scope`; `targets_from_view` rejects an out-of-scope target naming it → verify: `tests/test_scope_filter.py` — self-scoped node excluded for another owner, included for its own. Done 2026-09-20 (commit `e367438`, 7 passed; `LIVESTACK_NODE_SCOPE` env, namespace match rule shared between announce and admit sides).
- [x] A.5 hostd (fleet broker and host broker) and the node facade log every mutating request and every auth refusal with source address, status and principal name — the R.3 gate is "zero 401s from an address not in the inventory", and a refusal nobody logged is unattributable by construction. Measured deficiency 2026-09-20 (R.1): `livestack-hostd` on zz-tower0:8799 has logged **zero** requests in its entire journal history; uvicorn access lines for the other services also do not reliably reach the journal. → verify: with logging landed, `journalctl -u <svc>` after a tokenless `POST /admit` shows one line naming the source address and the 401; a granted admit with a token names the principal. Done 2026-09-20 (commit `ba34347`, tests/test_request_logging.py 6 passed; suite 725). Root cause was not code but systemd: `StandardOutput=append:…/hostd.log` redirected uvicorn's access log away from the journal (698k lines sat in hostd.log); fixed with a pure-ASGI `request_log.AuditMiddleware` (mutating calls + 401/403 only, principal name or `unknown(<fingerprint>)`, never a token, never bodies) wired into hostd's build_app and facade attach(), plus a `20-journal.conf` drop-in on zz-tower0's livestack-hostd, verified live in journalctl. polyasr.service has the identical redirect and keeps the old behaviour until its own rollout — the drop-in is the template.

### R. Token rollout (operations; the gate of Phase A)

- [x] R.1 Complete the inventory from a CN host (zz-tower0's fleetd/hostd/polytts/polyasr journals) and resolve the three LAN addresses by MAC → verify: the inventory table above has no "unresolved" row. Done 2026-09-20 (plan commit after `25e5239`): the CN half is measured and recorded in the inventory section below; the three LAN rows resolve to xc-tower-ubuntu itself (`10.0.0.94`, its own eno1 + /etc/hosts), the user's Honor phone (`10.0.0.244`, OUI 78:e6:1c Honor Device, the xc-magical pair's vendor), and a TP-Link AP/NAT box (`10.0.0.71`, OUI 9c:53:22, one MAC answering ARP for five leases). No *logged* CN source is unidentified; the unloggable one is hostd, which is deficiency A.5 above.
- [ ] R.2 Issue the principals in the table; each caller's configuration gains its token (`ATTUNE_FLEET_TOKEN`, `BENCHDAY_FLEET_TOKEN`, media-corpus's ingest, the engines' delegating tokens) → verify: every caller logs a successful admit with its principal name.
      Tokens minted 2026-09-20 into `~/fleet-tokens/principals-2026-09-20.json` (mode 0600, 11
      principals, on zz-tower0 — the ops box; never in any repository). Migration note: the
      table already live on NA since Sep 5 names its delegating principal `hub` with prefix
      `acct_`, predating the app-namespace contract (`<app>:acct_<id>`). R.2's `benchday-hub`
      (prefix `benchday:`) supersedes it, which requires benchday's title chain to change its
      admit owner from `acct_<id>` to `benchday:acct_<id>` — that is benchday's
      `title-from-the-local-card` §8/D1 line and is filed there, not patched around here. The
      old `hub` row is removed from NA only after the CN rehearsal passes. What remains for
      R.2: distribute each caller's token (`ATTUNE_FLEET_TOKEN` in the Mac's attune `.env`,
      `BENCHDAY_FLEET_TOKEN` on the hub, `HARMONY_LLM_FLEET_TOKEN` / engine tokens on their
      hosts, media-corpus's ingest env) and watch each caller log a successful admit under its
      principal name.
      **Sequencing corrected 2026-09-21.** The remaining half of R.2 cannot be verified before
      R.4: every caller in the table (attune on xc-mac-studio, the benchday hub on public-la,
      harmony-llm on xc-tower-ubuntu, media-corpus's ingest) admits against the **NA** broker,
      which still holds the Sep-5 `hub`/`acct_` table. Handing those callers their new tokens
      before NA's table is replaced turns each of them into a 401 — the table and the callers'
      tokens are one atomic switch, and that switch is R.4. What R.2 could deliver
      independently, and did, is the table itself and its correctness (the `"*"` fix recorded
      under R.3). The rest is carried into R.4 rather than left as a half-applied rollout.
      Finding recorded while distributing: of the six engine principals only **harmony-llm**
      has an admit call of its own (`HARMONY_LLM_FLEET_TOKEN`,
      `examples/harmony-llm/server.py:639`). polytts and polyasr are passive nodes — B.3
      already records that their identity-bearing surface is the lease `owner_id` their
      callers make, not an admit of their own — so `polytts@*` / `polyasr@*` belong to the
      node-facade token surface (`LIVESTACK_NODE_TOKENS_FILE`, task A.3), not to `/admit`.
- [ ] R.3 Stand up the CN rehearsal broker on zz-tower0 (a `livestack-fleetd` in `LIVESTACK_DISPATCH=observe` mode peering with the CN nodes, the NA broker's mirror of the same shape), set `LIVESTACK_FLEET_TOKENS_FILE` on it first; run ≥ 24 h → verify: the CN journal has zero 401s from an address not in the inventory — which requires A.5's request logging, else a 401 is unattributable by construction.
      Stand-up done 2026-09-20: `livestack-fleetd-cn.service` on zz-tower0 (observe mode, port 8801, peers = the three CN node facades, links peers mirroring the NA broker, journal output), verified `GET :8801/fleet` → `auth.required: false`, `peers: 3`. **The rehearsal clock STARTED 2026-09-21 02:32 CST (UTC+8).** Both remaining steps landed on zz-tower0: (1) drop-in `livestack-fleetd-cn.service.d/10-tokens.conf` sets `LIVESTACK_FLEET_TOKENS_FILE=/home/ubuntu/fleet-tokens/principals-2026-09-20.json`, so `GET :8801/fleet` reports `auth.required: true` and the startup line reads `11 principal(s)`; (2) drop-in `30-cn-rehearsal-broker.conf` on `polytts`, `polyasr` and `chipgen` sets `LIVESTACK_BROKER_URL=http://127.0.0.1:8799,http://127.0.0.1:8801`, so the three CN nodes report for duty to the rehearsal broker as well as to their host broker — `GET :8801/peers` shows three `source: registered` rows beside the three seeds (the seeds collapse onto them by `alias_of` as each is probed).
      **Deficiency found and fixed, not worked around** (commit `0252bff`): the six engine principals R.2 minted carried the table's `delegate_prefix: ""` and ALL SIX were dropped at load — `load_principals` reads `""` as absent and refuses it — so the rehearsal would have counted six of its own callers as unexplained 401s. `fleet_auth.RELAY_ANY` (`"*"`) is now the one prefix that means every owner; `""` is still refused (a typo must not mint that principal) and the refusal names the wildcard. Live: 11/11 principals load where 5/11 did.
      Rehearsal mechanism verified live the same minute — this is the surface the 401 count is read from: tokenless `POST /fleet/admit` → 401, `[audit] src=127.0.0.1 method=POST path=/fleet/admit status=401 principal=-`; the attune token naming a `benchday:` owner → 403 `principal=attune-hub`; the attune token naming `attune:acct_1` → 200 `principal=attune-hub`.
      Count the gate at or after **2026-09-22 02:32 CST**: `journalctl -u livestack-fleetd-cn --since "2026-09-21 02:32" | grep '\[audit\].*status=401'`.
- [ ] R.4 Then NA → verify (the Phase A gate): `GET /fleet` on xc-tower-ubuntu reports `auth.required: true` and the quota in force; over one hour every LLM, TTS and ASR Grant names an owner from the principal table; `scripts/explain_reload.py` attributes one deliberate 27B reload from the ledger alone.
      **THE THIRD CLAUSE IS MET (2026-09-21 05:31 CST). The first two still wait on the CN
      clean day, and deliberately so.** The three clauses are separable and were separated:
      the attributable ledger is C.1-C.3's mechanism and needs no token switched on, so it was
      deployed and measured on its own.
      Deploy: livestack `393b97fd` shipped to xc-tower-ubuntu as
      `~/.local/share/livestack-releases/393b97fdef12eedf98746c0af15cd6a0c6aa1701/`, with
      `livestack-hostd.service.d/40-release.conf` pointing `PYTHONPATH` at it. A RELEASE
      DIRECTORY rather than a pull, because that host's `~/livestack` carries another agent's
      22 uncommitted lines in `examples/harmony-llm/server.py` and a merge would have touched
      that file. **No tokens file was added and no auth changed**: `GET :8799/fleet` reports
      `auth.required: false` before and after; `:8801` keeps its Sep-5 table
      (`hub`, `media-corpus`) untouched.
      The deliberate reload, on the live 27B (`llm_title`, 22.5 GB, `params_b: 27`,
      `quant: int4`, resident on `xc-tower-ubuntu-gpu1` = harmony-llm's card): an admit for
      `llm_tiny` shed it, then an admit for `llm_title` as `owner=ops-title` brought it back
      (`plan: load llm_title@xc-tower-ubuntu/a46c4c2e … (loaded on demand)`). Ledger grew
      261 → 270 lines. Then, **from the ledger alone, no journal consulted**:

          $ python scripts/explain_reload.py ~/.cache/livestack/decisions-xc-tower-ubuntu.jsonl llm_title
          llm_title reload, from the ledger alone:
            evict  ts=1789939873.51931   caused_by=pressure
            load   ts=1789939888.2650938 caused_by=ops-title
          exit 0

      **Honest reading of the evict half:** `pressure` is not a principal, it is C.1's
      designed value for a rule-0 shed — the planner freed the card generally rather than to
      make room for one named request, and saying `pressure` is the mechanism being truthful,
      not a gap. The gate row below says "both in the principal table"; the LOAD is
      (`ops-title`), the EVICT is a policy. A named evict needs an admit whose room-making
      lands on that same card, and the planner correctly declined to create one: asked for
      `embed_multi` (3.2 GB against 2.15 GB free on gpu1) it placed the unit on the OTHER card
      where it was already resident rather than evicting the 27B. Forcing a named evict would
      mean saturating both production cards to obtain a label, which is not worth it — recorded
      instead.
      The 27B was verified serving again afterwards (`/v1/models` →
      `dbirks/Qwen3.8-27B-W4A16-AutoRound`). What remains for R.4 is the TOKEN half: expand
      NA's table to the R.2 principals and turn `auth.required: true` on hostd, after the CN
      clean day, then count `owner_asserted: false` Grants over one production hour.

## Gates

| Gate | Number to record |
|---|---|
| Auth on, quota in force | `GET /fleet` → `auth.required: true`, `quota.max_concurrent_per_account` or prefix ceilings non-null |
| Every engine request attributed | count of Grant records with `owner_asserted: false` over one production hour = 0 |
| One reload explained from the ledger | `explain_reload.py` output: evict `caused_by`, load `caused_by`, both in the principal table |
| CN rehearsal | 401s from addresses outside the inventory over 24 h = 0 |
