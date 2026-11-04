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

1. **The router is the placement point; the client is a transport.** A client sends one request to its region's router (`127.0.0.1:8188` on machines that have one; the hub's router for phones) and does nothing about readiness, busy windows or alternates. A phone cannot run a router, and **the hub is never in the audio path** (see "Where the router sits"): the router runs beside the EXISTING relay the phone already reaches.
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

## Where the router sits: on the relay path, never in the hub (owner correction 2026-11-04)

Facts, checked in benchday `origin/dev`:

- **Phone ON the tailnet.** The hub manifest gives owners a `direct` route: `ws://<mesh host>:<port>/ws/transcribe` and `http://<host>:<port>/v1/audio/transcriptions` (hub/src/speech_relay.ts:580-600). Audio: phone -> engine, no relay, no hub. The client picks among routes itself (packages/asr_client/lib/asr_client.dart `_ranked`/`pickBest` ~:633-650).
- **Phone OFF the tailnet (China cellular).** The manifest gives `regional_relay` routes (speech_relay.ts:603-641): `<relay base>/speech/asr/<target>/ws/transcribe?cap=<bdsr1 token>`. Audio: phone -> nearest relay (zz-tower2-cn, `https://tower2.zztech.cc:3389/benchday-relay`, region asia-cn; or hto-histo-na on histo-one for NA: scripts/deploy-regional-relays.sh:55-73) -> engine over the mesh at the relay's configured upstream (`ASR_TARGETS`, hub/src/regional_relay.ts:270, route table :366-370). The relay is a separate process on the edge host; it runs hub code but is not the hub. The hub only mints the token and orders the list.
- **The hub does hold audio in one place today**: fire-and-forget intents (hub/src/intent-queue/recognize.ts) spool a WAV and POST it to a relay batch route with a hub-minted `speech.asr.batch` cap. That is an existing, separate feature; this change adds no new audio at the hub and the router does not change it. (It does mean "the hub never sees audio" is not true today, and the owner should know.)
- **Vendor targets** (relay_qwen_asr.ts) send audio from the relay to the vendor; unrelated to this change, listed for completeness.

Where the router goes:

| Case | Today | After |
|---|---|---|
| China phone | phone -> zz-tower2 relay -> one named engine | phone -> zz-tower2 relay -> **router on zz-tower2 (loopback)** -> best engine |
| NA phone | phone -> histo relay -> one named engine | phone -> histo relay -> **router on histo-one (loopback)** -> best engine |
| Phone on tailnet | phone -> engine direct | unchanged (client keeps its own failover) until it is given a relay/router route; see below |
| Hub intent WAV | hub -> relay -> engine | hub -> relay -> router -> engine |

The relay's upstream map gets ONE logical target per region (`harmony-asr`, upstream `http://127.0.0.1:8188`) instead of one entry per engine. The router lives on the same host as the relay, so it adds a loopback hop (about 1 ms) and no round trip to another continent; ranking uses the router's cached fleet view (10 s TTL, bounded stale), not a per-request network call. A hub in Tokyo or LA would add a wrong-continent round trip for a China phone: the hub is not used.

Capability tokens map one to one and need no change: the relay (not the router) is the auth boundary. `speech.asr.stream` -> relay `asr_ws` -> router `/ws/transcribe`; `speech.asr.batch` -> `asr_batch` -> router `/v1/audio/transcriptions`; `speech.asr.health` -> `asr_health` -> router `/health`. The cap names a `targetId` that the relay resolves in its map; with one logical target the hub mints caps for `harmony-asr` per relay. The router binds loopback and holds no credential (existing router rule). `speech.tts.fetch` has no router mount yet (TTS slice).

Gaps to close before any cutover (none implemented here):
1. **Router health must mean "can serve".** The relay's health probe and `asr_health` call upstream `/health`; today the router answers `ok` regardless of engines. Needed: a speech-aware readiness (`HARMONY_ROUTER_HEALTH_KIND=polyasr` -> 503 + reason when no eligible node).
2. **Relay WebSocket bridge to the router** must forward `?query` and the `start` frame untouched (the router already does); verify with the relay's e2e lane.
3. **Routers on the relay hosts**: zz-tower2 already runs one (docs/livestack-harmony.md "Where to point base_url"); histo-one has none. Rolling a router onto histo-one is a deploy decision for the owner; zz-tower2's hostd is not touched.
4. **Tailnet phones** keep direct routes; the client busy logic therefore stays for them. Either accept that, or give them the relay route (adds a hop for a phone that could go direct).

## Migration (revised): the client's skip/retry logic is deleted only after its traffic goes through a router

Order, each step shippable alone:

1. Land router health (gap 1) and add the `harmony-asr` logical target to the relay maps (hub settings / deploy script), router already on zz-tower2.
2. Hub manifest: for off-tailnet accounts emit the `harmony-asr` relay route FIRST and keep engine-named routes after it as fallback. Hub change is control-plane only (token + route hint).
3. Observe: placement log outcomes (success, cause, first_byte/first_partial/final) for the relay path vs the old path.
4. Only then delete, for the relay path: `endpoint_busy.dart`, `_skipBusy` and the busy book wiring listed below. For a client that still has direct routes (tailnet) the busy book stays; so the deletion is `relay-route-only` code, i.e. effectively nothing can be deleted until tailnet phones also use a router route or the owner accepts their failover being client-side. Deleting is therefore NOT part of this change's first cut.

Files/lines that become deletable once no client route is engine-direct (from `origin/dev` ecbf16cf24): `packages/asr_client/lib/src/endpoint_busy.dart` (whole) and `test/endpoint_busy_test.dart`; `endpoint_health.dart` `_noteBusyFromHealth` (:58), `_noteBusy` (:68), `_noteBusyFromConnectError` (:84), `_skipBusy` (:93); in `asr_client.dart` the `_busy` book and `capacityBusyMessage` (:325-331), `_skipBusy(...)` (:639), `_busy.hasActive` (:647), deprioritize guard (:1738), `_noteBusy` (:1901), `_noteBusyFromConnectError` (:2106), import/export (:14, :24-25); `ws_failover.dart:100`; `batch_post.dart` busy field (:36-44, :150); `lib/providers/asr_provider.dart:1501-1509,1606`. The hub `asr_outcomes.cause` column stays. The hub manifest ordering (`buildSpeechRouteManifest` `orderByRanking`, `fleet_rank.ts`, the pushed rank stream) stays as the fallback ordering for engine-named routes.

## Privacy: what the placement log may hold

The router never parses audio and never puts the WS `start` frame (which carries the `context` string: pane names, recent turns) or any request body into a placement problem. A speech problem's `request` is `{}`; candidates carry fleet node rows (readiness, load, region, inventory such as model names and voice ids); outcomes carry target, success, a short cause, latencies, attempt results. The audio replay buffer (uncommitted WS sessions, <= 4 MiB) lives in router memory only and is dropped at commit or close. Asserted by `test_the_decision_log_holds_features_and_outcomes_never_audio_transcript_or_context` (WS, with failover) and `test_batch_asr_logs_never_hold_the_audio_or_the_form_fields` (HTTP): sentinel audio, context and transcript strings reach the nodes (positive control) and appear in no byte of the real placement or route log files. Residual risk: an engine's own error text is logged as `cause` (truncated to 200 chars); engines do not put content there today, and the cause is for the operator.

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

## Streaming ASR as built (slice 3)

`router.py` `/ws/transcribe`. A node acks `started` immediately and admits afterwards (refusal = error frame + close 1013), so a session is UNCOMMITTED until the first message that is not `started`/`resumed`/`warming` (an `ack`, `partial` or `final` proves the node admitted and is working). While uncommitted the router keeps every client frame (cap `HARMONY_ROUTER_WS_REPLAY_MAX_BYTES`, 4 MiB; past it the session is final) and a refusal or connect failure re-places on the next provider, swallows its `started` and replays the frames. After commit a drop is the client's own `resume`. `asr.streaming=native` is a default PREFERENCE. Measured 2026-11-04 (router-trial on xc-tower-ubuntu against the live na fleet, tower refusing `no device can fit even with preemption`): tower ranked first and refused, zz-joe served; client ack 20-40 ms, failover cost under 300 ms, warm first partial 0.74 s of audio-time, final text correct; cold zz-joe model load 13.5 s (announced by `warming` frames).
