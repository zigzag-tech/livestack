## Context: where the cross-host decision is made today (evidence 2026-11-04)

| Decision | Where | Covers speech? |
|---|---|---|
| Which node should START this kind, from this vantage | `fleet_rank.rank()` (node-py/livestack_node/fleet_rank.py:370), served as `GET /fleet/rank` (hostd.py:749) | Yes: `_serves()` matches node `kinds` (`polyasr`) and unit names (`asr`); `ready:false` -> cold/held back (:467); distance band from `region:<r>` (:133, :168); load (:111); `require=voice:` inventory (hostd.py:824) |
| Route + forward + retry an HTTP request | `router.py` `_route`, `fleet_route.choose`/`send_routed` | No: `ROUTABLE` (router.py:199) excludes audio; `_plan` derives only llm/embed (:115); `send_routed` retries 409/connect/429 only |
| Ordering a client dials | hub `speech_relay.buildSpeechRouteManifest` (hub/src/speech_relay.ts:446, `orderByRanking`) fed by a pushed `fleet_rank` stream (hostd_streams.py:120, `BENCHDAY_FLEET_RANK_PAIRS`) | Advisory order only; TTL'd; falls back to the static `ASR_TARGETS` order |
| Connect, probe, quarantine, busy window, failover | `packages/asr_client` (`asr_client.dart`, `endpoint_health.dart`, `endpoint_busy.dart`, `ws_failover.dart`, `batch_post.dart`) | This is the duplicated placement |

Live read: the tower's polyasr row is `ready:false, detail: "device held exclusively by harmony-image"` while the Mac (cn, Qwen3-ASR-1.7B MLX, `streaming: synthetic`, measured `finalization_ms` 5.0 s, failure 0.0), zz-joe and zz-whitby (na, Qwen3-ASR-1.7B) are ready. The tower's Confucius4-R2T2 is `streaming: native` with measured `failure` 0.17 and `finalization_ms` 23.6 s. All four facts already sit in the roster `inventory.asr`.

So the answer to "can Harmony route speech cross-host with configuration or a small wiring change": ranking, readiness-with-reason, region distance and load: **already**. Acting on them (forward, retry, exhaustion reason): **a small router change for HTTP**, done in slice 1. Streaming ASR (a WebSocket) and TTS (not under `/v1`) need their own mounts.

## Decisions

1. **The router is the placement point; the client is a transport.** A client sends one request to its region's router (`127.0.0.1:8188` on machines that have one; the hub's router for phones) and does nothing about readiness, busy windows or alternates. A phone cannot run a router: it talks to the hub, and the hub's router places (the hub already holds the tailnet reach). The manifest then carries one stable entry (the hub/relay door), not an ordered list of engines.
2. **Failover only before the first byte reaches the caller.** Batch ASR: the whole upload is held by the router (bounded by `max_request_bytes`), so a retry replays the same body. Streaming ASR: retry only while the session is still unopened (`start` not yet acknowledged, no audio consumed by a node); after that a drop is the client's `resume` path, which carries `recognizedThroughSeq` and is not placement.
3. **Busy is not an outage.** A `503` with busy advice marks the target busy in the cached view for the advertised window (clamped 1..300 s, as the client clamps today) and is not counted as a failure of the model; a bare `503` is retried the same way but never labelled busy. Neither is retried against the same target in one request.
4. **Budget is attempts, not time.** Default 3 attempts, `HARMONY_ROUTER_SPEECH_ATTEMPTS`. Each try is one HTTP round trip to a node that already answers `/health` in milliseconds when busy (the node refuses before loading), so 3 attempts is bounded by 3 RTTs, not by a model load. A node that must load (cold) is expected to say `warming`; the router does not retry across a warm-up.
5. **Exhaustion names every try.** `route_failed` + `attempts[]` + `Retry-After`. Clients render `speech capacity busy: <reason>` from the body (today they synthesize it from their own busy book).
6. **Ranking inputs, in order** (all but the last two already exist): hard requirements (`kind`, `voice`, `streaming`, model tier clause) -> readiness (a node that reports a device lease is filtered with its reason, not held as "cold") -> region policy (`harmony_regions`/principal allowance) -> distance band from the caller's vantage -> load -> `target_id`. **New, later:** (a) **measured path** per client region: today `distance_to()` uses the fleet links table (median of region hosts' link rows). A DERP-relayed path the links table does not see (tower <-> Mac 745 ms ping, 13-21 s for a 5 s clip) must show up as a measured distance, via the same `links` rows the meshlink probe already publishes, not a router constant; (b) **quality tier**: `inventory.asr.model`/`backend` are strings; a `tier` clause needs an ordered attribute declared by operators on the unit, never inferred from a model name.
7. **Region policy is the owner's** (open question below). The mechanism exists (`harmony_regions`, principal `regions` ceiling). What is not decided is the default for a China-network phone: Mac via the zz-tower2 relay vs the tower.
8. **Readiness field for a held device (later slice).** `process_node.capability()` reports `ready:false` + `detail` text only; `fleet_admit` already special-cases the string `"no unit resident"` to tell cold from unready, `fleet_rank` does not. Add an explicit `device_busy` string to the capability, copy it in `hostbroker` view rows, and filter on it in `fleet_rank` (`filtered: device busy (<reason>)`) so a leased node is never the "cold fallback" when it is the only node. Until then, the router's busy retry surfaces the same reason in `attempts[]`.

## Streaming ASR and TTS (not in slice 1)

- **Streaming ASR** is `ws://host:port/ws/transcribe`. A router WebSocket mount accepts the client socket, ranks, opens an upstream socket and relays frames; a `1013`/`error` frame before `started` (process_node `refuse()` uses close 1013 + `{"type":"error"}`) triggers re-rank excluding that node. Audio the client sent before the ack is buffered in the router (bounded: the client already spools). The `start`/`resume` messages and sequence numbers are forwarded untouched, so the existing resume protocol keeps working across a router.
- **TTS** is `/tts/stream` and siblings on polytts, not under `/v1`. Add a `/tts/{path}` router mount mapped to kind `polytts`, with `voice` taken from the JSON body (`voice_id`) and passed as `require=voice:<id>` to the existing inventory match (hostd.py:824); a node that does not advertise the voice is never a candidate.

## Migration: what the benchday client/hub delete once the router places speech

Prerequisite: the speech traffic of a client flows through a router (own or hub's) and `route_failed` bodies are in production. Then:

- `packages/asr_client/lib/src/endpoint_busy.dart` (whole file: `BusyAdvice`, `EndpointBusyBook`, `busyAdviceFromResponse`, `probeBusyAdvice`, `userFacingBusyMessage` moves to rendering `route_failed.attempts[*].result`) and `packages/asr_client/test/endpoint_busy_test.dart`.
- `packages/asr_client/lib/src/endpoint_health.dart` busy parts: `_noteBusyFromHealth` (:58), `_noteBusy` (:68), `_noteBusyFromConnectError` (:84), `_skipBusy` (:93), and their callers in `asr_client.dart`: field `_busy` + `capacityBusyMessage` (:325-331), `_skipBusy(...)` (:639), `_busy.hasActive` (:647), `_busy.isBusy` deprioritize (:1738), `_noteBusy(ep, busy, 'http_batch')` (:1901), `_noteBusyFromConnectError` (:2106), the `import`/`export` of `endpoint_busy.dart` (:14, :24-25), and `ws_failover.dart:100`.
- `packages/asr_client/lib/src/batch_post.dart`: the `busy` result field (:36-44) and `busyAdviceFromResponse` call (:150).
- `lib/providers/asr_provider.dart:1501-1509,1606`: `capacityBusyMessage` and the `cause: capacity_busy` event are derived from the `route_failed` body instead. The hub `asr_outcomes.cause` column (`hub/src/asr-outcomes.ts:34-37,97,144`) **stays**: it is a telemetry cause, now set from the router's reason rather than the client's book.
- The picker's quarantine/RTT ranking (`asr_client.dart` `_maxFailoverAttempts`, `orderForReachability`, health probes) shrinks to "reach the door" once the manifest carries one entry; delete only after streaming ASR routing lands, because it still serves the WebSocket path.
- Hub `speech_relay.buildSpeechRouteManifest` + `fleet_rank.ts` ordering + the pushed `fleet_rank` stream become unnecessary once the router is the placement point; remove after the client no longer reads engine order.

Order matters: do not delete the client busy book while any speech path (streaming, TTS) is still client-placed.

## Open questions for the owner

1. China-network phone default: Mac Studio via the zz-tower2 relay (Qwen3-ASR on MLX, `streaming: synthetic`, 0.9 s local on a 5 s clip, path from the phone unmeasured) or the tower (Confucius4-R2T2, `streaming: native`, first partial 0.5-1.2 s, but 3.1 s probe and image jobs hold both GPUs for 15-30 min)? Rank by measured distance and let region policy express the preference, or pin a preference per account?
2. Does "streaming" mean native partials (a hard requirement, which excludes the Mac) or an acceptable synthetic stream (a preference)? Slice 1 states neither.
3. Is 3 attempts / 30 s default busy window the right budget for dictation, where a second of dead air is felt?

## Placement is a problem, not a function of speech (owner requirement, 2026-11-04)

Everything above is expressed through `placement/v1` (livestack `HARMONY.md`,
"Placement as a problem"): a versioned `problem {kind, request, candidates[{id, facts, eligible}], context}`
answered by a pure `Ranker`, selected per kind by config, with `default-v1` (the existing
`fleet_rank` ordering, unchanged) as the only ranker built. Decisions:

1. **Eligibility is separate from ranking.** `problem_from_fleet` stamps `eligible` +
   `ineligible_reason` from the hard rules (kind, voice/model clause, region policy,
   fresh, not leased via `device_busy`). Rankers see only eligible candidates and can
   only reorder them; illegal output is replaced by the default.
2. **Never silent.** Exception, timeout (0.25 s), unaccepted schema, unregistered name,
   garbage -> `default-v1` plus `fallback{from,why}` on the decision and in the log.
3. **Forward compatible.** Unknown fields ignored by rankers, preserved in the log;
   namespaced context keys (`net.rtt_ms`, `acct.prefs`); rankers declare accepted schemas.
4. **Instrumented for learning, nothing learned.** Decision and outcome records in a
   bounded `DemandLog`; shadow ranker support; `placement.replay` interface with a trivial harness.
5. **Owner decisions (2026-11-04):** China-phone default = rank by MEASURED distance plus region
   policy (no per-account preference); native streaming is a PREFERENCE (`asr.streaming=native`
   in `harmony_prefer`, existing attribute), never a requirement; 3 attempts, busy window = node
   `Retry-After` clamped 1..300 s.
