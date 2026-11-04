## Why

Speech is the one inference class Harmony ranks but does not route. `fleet_rank.rank()` is kind-generic and already answers, for `polyasr`/`polytts`, the question "which node, from this vantage": it drops a node whose device an image lease holds (`ready:false`, `detail: device held exclusively by harmony-image`), prefers the nearest distance band from `region:<r>`, breaks ties on load, and matches `voice`/model inventory. Verified live 2026-11-04 against the na fleetd: `GET /fleet/rank?kind=polyasr&vantage=region:na` ranks zz-joe, zz-whitby, then the Mac (1337 ms) and omits the tower while FLUX holds its GPUs; `vantage=region:cn` ranks the Mac first and the tower last (3154 ms).

But nothing acts on that ranking for speech. The harmony router refuses any `/v1` path except chat, completions, embeddings and classifier (`router.py` `ROUTABLE`), derives only the `llm`/`embed` kinds (`router.py` `_plan`, `request_language.derived_requirements`), and retries only `409 route_stale`, a pre-response connection failure or `429` (`fleet_route.send_routed`). The benchday hub turns the same ranking into a *manifest order* and the client dials, probes, quarantines and fails over on its own (`packages/asr_client`). So placement is decided twice, a client restates a 503 policy Harmony already knows (`Retry-After`, the planner's reason), and a client that cannot see the fleet view (a phone) has no way to prefer the Mac over the tower except a static list.

Owner's rule: clients should be dumb; Harmony keeps allocating and, if it must, retries on other resources.

## What Changes

- **Declarative speech request.** A speech request states what it needs, not where: the endpoint implies the kind (`/v1/audio/transcriptions` -> `polyasr`), and optional clauses (`streaming`, model tier, `voice`) travel as `X-Harmony-Requires` (the existing `require:` grammar) because the body is audio. The router ranks the fleet from its own region vantage and forwards. **Slice 1 (implemented here):** batch ASR through the router.
- **Server-side failover with a bounded budget.** For speech the router treats a pre-byte `503` as a refusal to fail over from: `busy: <planner reason>` when it carries busy advice (`device_busy` body, `Retry-After`, or "did not grant this device"), `unavailable: ...` otherwise. It excludes the target, re-ranks, retries up to `HARMONY_ROUTER_SPEECH_ATTEMPTS` (default 3), and drops a busy target from its cached view until the next refresh. LLM routing is unchanged (retry stays at 2, 503 not retried).
- **A reason on exhaustion.** When no provider can serve, the caller gets `503 {"error":"route_failed","attempts":[{target,result}]}` naming every try and the planner's words, with `Retry-After` set to the shortest advertised busy window.
- **Ranking is not widened in slice 1**, only made applicable to speech: the default `class=<kind>` requirement applies to `llm`/`embed` only, because speech units declare no `class` attribute (it filtered every speech node).
- **Later slices** (design.md): streaming ASR through the router (`/ws/transcribe`, retry at session open only), TTS (`/tts/stream`, not under `/v1`), measured-path ranking inputs, the `device_busy` readiness field, benchday client/hub simplification.

## Impact

`node-py/examples/harmony-llm/{router.py,fleet_route.py}`, `node-py/tests/test_harmony_router.py`; later `process_node.py`, `hostbroker.py` (view row), `fleet_rank.py`. Benchday follow-up deletes the client busy book (see design.md "Migration"). No change to a node's own admission, to `/fleet/rank`, or to any LLM request.
