# Route selection — a livestack capability

Status: **core landed** (`shared/src/route.rs`, 39 tests) + **TS/wasm binding
landed** (`shared-wasm/src/route_wasm.rs`). Python and Dart bindings are the
remaining work; see "Bindings" below.

## What this is

A client choosing the fastest *physical path* to a named service — direct, via a
regional relay, via a hub proxy, or P2P — and re-choosing it when that path
degrades. The engine owns per-candidate health, learned task latency (EWMA with
age decay), quarantine, transport cooldown, exploration, failover exclusion, and
a pessimistic bootstrap so an unmeasured node cannot dethrone a proven one on a
snappy ping alone.

It performs **no I/O and reads no clock**. Probing is the host's job; results
come back through `record_*` hooks and time arrives as `now_ms`. Same doctrine as
`shared/src/residency.rs`, and for the same reason: one brain, deterministically
testable, bound rather than reimplemented per language.

## What it is NOT

**Client → endpoint selection is not hub → worker placement.** Which GPU host
runs a job is the `ExecutionBackend` / capability-lease lane. Both may be
livestack capabilities; they do **not** share a scoring model, and merging them
produces exactly the wrong answer for jobs that span both — a pane announcement's
digest *generation* is worker placement while its TTS/ASR audio is endpoint
selection.

This supersedes the scope note in benchday `docs/mesh-route.md`, which said the
selection engine must stay out of livestack. That ruling was written when
"in livestack" would have meant "merged into the placement lane". It does not
here: the two arrive as separate capabilities with separate types. The boundary
the note protects is preserved; only its conclusion about where the code lives
has changed.

## Why it had to move

benchday had three independent implementations of "pick the fastest reachable
endpoint" — daemon route, narration TTS, and ASR — with *divergent and
complementary* sophistication (one had warm-up probe budgets, another had
first-audio EWMA, another had quarantine). `packages/mesh_route` collapsed those
three into one Dart engine. Leaving it there means the next TS or Python consumer
forks a fourth. Promoting it to `livestack-shared` is what makes the collapse
hold across languages instead of only within one app.

## Shape

```
manifest (JSON)  ──parse_route_manifest──▶  RouteEntry[]
                                              │ candidates_from_entries
                                              ▼
        record_reachable / record_task_latency / …  ──▶  Picker  ──▶  pick_best
                                                            │
                                                            └──▶  snapshot()
```

### The manifest is part of the capability

An engine with no candidate feed just re-forks the feed instead. livestack owns
the schema:

```json
{ "<targets_key>": [ {
    "target_id": "asr", "label": "ASR",
    "routes": [
      { "route": "direct", "priority": 10,
        "health_url": "http://100.64.0.3:8766/health",
        "ws_url": "ws://100.64.0.3:8766/ws" },
      { "route": "regional_relay", "relay_id": "cn-1", "priority": 20,
        "health_url": "https://relay1/health" }
    ] } ] }
```

- Picker keys are `targetId:route[:relayId]` — **relay-aware**, so two relays to
  the same target keep distinct stats instead of colliding on one key.
- Any string field ending in `_url` is collected, so adding a transport (`grpc_url`,
  `quic_url`) needs no schema change here.
- A producer emits this; the engine consumes it. A hub is one producer, not the
  definition.

### Transport-agnostic by construction

Nothing in the engine knows about WebSockets. A candidate is
`(key, kind, priority, probe_host)`; how you dial it and how you measure it are
the host's business. Even the URL validator takes an `EndpointPolicy` — the
reference implementation hardcoded `ws`/`wss`, and making the scheme set a
parameter is what turns a WebSocket gate into a general one:

```rust
parse_direct_endpoint(Some("https://100.64.0.3/health"), &EndpointPolicy::http())?;
parse_direct_endpoint(Some("grpc://100.64.0.3:50051"), &EndpointPolicy::any_scheme())?;
```

### Observability is the same object, not a bolt-on

`Picker::snapshot(now_ms)` returns what is selected, the ranked order, and the
evidence behind each position (state, score, RTT, learned latency, quarantine
deadlines, sticky flag). Emit it on change and a client renders its live path —
which node, which route class, measured RTT — and animates failover, without the
UI reimplementing any ranking logic.

Snapshot is observation-only: it does **not** advance the exploration counter, so
polling it cannot perturb selection. There is a test asserting exactly that.

## Bindings

The algorithm is bound per language, never reimplemented.

| Target | Status | Where |
|---|---|---|
| Rust | landed | `shared/src/route.rs` |
| TS / Node / browser | landed | `shared-wasm/src/route_wasm.rs` → `@livestack/shared-wasm` |
| Python | not started | mirror `shared-py`'s residency pyo3 wrapper |
| Dart / Flutter | not started | see below |

`Vec<T>` of tsify structs cannot cross the wasm boundary, so collections travel
in named wrappers (`RouteCandidateList`, `RouteEntryList`). Wire field names stay
`snake_case` to match the manifest (`target_id`, `health_url`, `regional_relay`)
rather than following the camelCase of the older graph bindings.

### The Dart binding is deliberately last

benchday's `packages/mesh_route` is the origin of this code and its most
load-bearing consumer — three call sites, a 453-line test corpus, and by its own
docs "the most-debugged area of the app". Swapping it for an FFI binding means
per-platform cdylib builds (android arm64/x86, ios, macos, linux, windows) in a
shipping app.

Until that lands, benchday keeps its Dart implementation and the two are kept
honest by the shared conformance corpus: the Rust tests in `route.rs` are a
direct port of `mesh_route_test.dart`, case for case. Any behavioural change must
land in both or in neither. That is a weaker guarantee than a single binary — it
is the correct trade while the Rust substrate work is still ahead of us, and it
is *why* the port kept the reference semantics exactly rather than improving
them in passing.

One deliberate difference: sorting here is **stable**, so equal-ranked candidates
keep input order. Dart's `List.sort` is not stable. Ranking is therefore strictly
more deterministic in Rust; no conformance case distinguishes them.
