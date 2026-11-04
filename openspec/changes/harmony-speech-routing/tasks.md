## 1. Slice 1 — batch ASR through the router (done in this change)

- [x] 1.1 `router.py`: `SPEECH_PATHS = {"audio/transcriptions": "polyasr"}` in `ROUTABLE`; `_plan` returns that kind with no requirement. Verify: `tests/test_harmony_router.py::test_a_busy_speech_node_is_failed_over_to_the_next_provider`.
- [x] 1.2 `fleet_route.choose`: the default `class=<kind>` clause applies to `llm`/`embed` only. Verify: same test (a speech node has no `class` attribute and is chosen).
- [x] 1.3 `fleet_route.send_routed`: `max_attempts`, `retry_unavailable`, `on_busy`; `busy_advice()`; `RouteFailed.retry_after_s`; router sets `Retry-After` on exhaustion. Verify: tests for busy failover, plain 503 failover, exhaustion body + `Retry-After` = shortest window, budget of 3 over 5 busy nodes, no-node message, LLM path unchanged.

## 1b. Placement as a problem (owner requirement)

- [x] 1b.1 `placement.py` (schema, registry, budgeted `run`, fallback, shadow, bounded log, outcomes, replay) and `placement_default.py` (`problem_from_fleet`, `default-v1` over `fleet_rank`); `fleet_route.choose` goes through it; router logs outcomes. Verify: `tests/test_placement.py`, router and rank suites unchanged.
- [x] 1b.2 HARMONY.md section with two worked examples.

## 2. Slice 2 — readiness says why (small)

- [x] 2.1 `process_node.capability()` adds `device_busy`; `hostbroker` view row copies it; `fleet_rank` filters on it with its reason (never the cold fallback). Verify: rank test "only node, leased -> filtered with reason, not chosen".
- [ ] 2.2 Optional `X-Harmony-Requires` header on speech requests parsed with `request_language`; `streaming=native|synthetic` as a unit/inventory attribute. Verify: router test narrowing by `streaming`.

## 3. Slice 3 — streaming ASR and TTS mounts

- [ ] 3.1 Router websocket `/ws/transcribe`: rank, upstream open, re-rank on `1013`/`error` before `started`, frame relay, bounded pre-ack buffer. Verify: fake nodes, one refusing at open.
- [ ] 3.2 Router `/tts/{path}` mount to kind `polytts`, `voice_id` -> `require=voice:`. Verify: voice on one node only.

## 4. Slice 4 — measured path

- [ ] 4.1 Publish client-region -> node distance from probes the DERP-relayed path actually experiences (the links table), so `region:cn` does not rank a relayed host above a direct one. Verify: rank test with a 745 ms row.

## 5. Benchday migration (separate change in benchday: `speech-placement-in-harmony`)

- [ ] 5.1 Hub speech manifest carries the router door only; client reads `route_failed` bodies. 5.2 Delete the files/lines listed in design.md "Migration". Verify: `asr_client` tests that assert failover on busy are replaced by router-level tests.
