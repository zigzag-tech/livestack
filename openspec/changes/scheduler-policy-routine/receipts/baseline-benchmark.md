# Baseline receipt: Harmony routing decisions vs a Jingway "hot policy" tier

Measured 2026-09-24 00:37–00:51 EDT. **Measurement only**: no repository was modified. `git status` stayed clean in `~/livestack` and `~/jingway` on both hosts. No service was restarted, reconfigured or load-tested. The live probe sent exactly 40 requests.

Every number is a **median / p90 / p99 in µs** unless marked otherwise. `n` is the sample count. Each sample is one call, unless the row says `inner=k`: then each sample is the mean of k back-to-back calls, and that is used only for sub-µs operations. Warm-up runs before every row. Python GC stays enabled.

## 0. Record fields (per `~/jingway/docs/native-acceleration-decision-template.md`)

| field | value |
|---|---|
| candidate | `harmony-routing-policy-evaluator` (planner.plan / fleet_rank.rank / fleet_scheduler.schedule / server.py request matching) |
| workload | the prod xc-tower-ubuntu host world (2 GPUs, 16 units, from a real `host-broker/grant` ledger record) plus synthetic fleets built with the livestack tests' own constructors, scaled to 1/5/20 hosts |
| materialityPercent | resident-reuse **~0.2 %** · warm admission **<10 %** (not material) · offline replay **~55–65 %** (material) — see §5 |
| baseline.hardware | **tower**: xc-tower-ubuntu, Intel i5-9400 @2.90 GHz, 6 cores, 92 GB, Linux 7.0.0-31. Production Harmony host, **noisy**, so loadavg is logged per run. **mac**: xc-mac-studio, Apple M4 Max, 14 cores, 36 GB, macOS 26.3. It is shared by other agents: load reached 22 during one run. |
| baseline.dataset | `fixtures/`: real `/etc/harmony/llm-units*.json` (+ 6-unit backup), the real LoRA `adapter_config.json`, and 3 median-size real ledger records (`real-ledger-records.json`) |
| buildProfile | Python: CPython (no build). `shared_py`: the production wheel already in `~/harmony-llm/venv` (cp312, pyo3 0.28.3). jingway python-bridge: `maturin build --release --locked`, abi3-py38, pyo3 0.23, 15 s build. wasm-bridge: prebuilt `pkg/nodejs`, 2026-09-08. The `wasm-bridge/src` and `core/src` sources were last committed in f3b0eed on 2026-09-08. The pkg was not rebuilt. |
| versions | jingway **4a43d095fcbfefdc518bac4011c80a98fe9bb87b** and livestack **4a3e0cb9164174a4a5f37a51e8e3f1d06e8152de** — **identical on both hosts**, so no worktree was needed and none was created. Python 3.12.2 (tower, harmony venv); Python 3.12.12 (mac, uv venv); Node v24.13.0 (mac); rustc 1.98.1 / cargo 1.98.1 (mac); httpx 0.28.1; @electric-sql/pglite 0.5.4 |
| boundaryCosts | measured in §4: PyO3 call ≈ 40–80 ns; marshalling 16 tuples in ≈ 1.85 µs; WASM + serde-wasm-bindgen object marshalling ≈ 5 µs, which makes it **slower than JS** |
| samples | the raw per-row JSON is in `out/*.json` (tower) and `out/mac/*.json` |

### Positive controls (the harness reads known costs correctly)

| host / harness | empty call | busy-wait 5 µs | busy-wait 50 µs | sorted(1000 ints) |
|---|---|---|---|---|
| tower, Python | 0.14 / 0.15 / 0.20 | 5.30 / 5.37 / 5.79 | **50.39** / 50.60 / 51.14 | 4.31 |
| mac, Python | 0.08 | 5.12 | **50.17** / 50.21 / 50.25 | 2.00 |
| mac, Node (hrtime) | ~0.00–0.04 | 5.04 | **50.04** / 50.08 / 50.17 | — |

Two more controls check that each native/pure comparison computes the *same* thing:

- The PyO3 `compute_hashes()` output equals a pure-Python re-implementation (`('-gh5jzf','cpjj9l')` on both sides).
- WASM `requestHash` equals the verbatim JS oracle on all 6 fixture cases.

Timer overhead on the tower is 0.14 µs per sample, so rows below ~1 µs use `inner=` batching.

## 1. Section A — Harmony decision path (Python)

### A1. `server.py` request parsing and matching

`server.py` cannot be imported safely on a production host. Import constructs the FastAPI app, `attach()` announces to a broker, and it starts a reaper thread. With `WARM_ON_START` it also calls `manager.ensure()`, which can spawn vLLM. `bench_server.py` therefore **extracts the pure functions from the AST** and execs them: 13 functions, byte-identical bodies. It never imports the module.

Every function the task named was measured. Nothing that does I/O (`_vllm_up`, `_held_elsewhere`, `admit`) was imported; those are covered in A6.

Tower, production units file (`units-prod`: 1 unit, `llm_general`, with a LoRA adapter):

| op | tower med / p90 / p99 | mac med |
|---|---|---|
| `_requirement_from` legacy `model=local` | 0.72 / 0.77 / 0.93 | 0.29 |
| `_requirement_from` `require:class=llm,family=qwen,params_b=[20,30)` | 10.34 / 11.01 / 14.52 | 3.92 |
| `_requirement_from` `harmony_requires` dict | 2.54 / 2.71 / 3.21 | 1.00 |
| `_derived_requirements` (any body) | 0.57–0.71 | 0.21–0.29 |
| `_attributes_for(llm_general)` | **32.37** / 33.13 / 39.43 | 17.08 |
| of which `_adapters_for` (**opens and parses `adapter_config.json` on every call**) | **27.35** / 28.15 / 33.64 | 15.25 |
| `_local_satisfies(llm_general, 4-clause req)` | **38.08** / 39.06 / 46.52 | 17.17 |
| `sorted(SPECS, key=_selection_rank)` 1 unit / 6 units | 0.41 / 1.15 | 0.17 / 0.46 |
| **proxy() pure decision**: `json.loads(body)` → requirement → derived → named → pick | **47.1–70.9** (p99 55–118) | 19–24 |
| same, units file without the adapter | 8.6–27.4 | 3.0–7.8 |
| same, 6-unit historical file (no adapter) | 11.4–29.0 | 3.6–8.1 |

A repeat run on the tower agreed within 3 % (for example, 46.87 vs 47.12 µs).

**Finding:** on production config, about 60 % of the pure request-matching CPU is re-reading one LoRA `adapter_config.json` from disk, once per `_local_satisfies` call. That is a caching fix, not a language fix.

### A2. `planner.plan()`

Synthetic worlds use `tests/test_planner.py` constructors: `gpu()`, `units()` and `_llms()`. About one idle resident per device, a third of them busy, a demand map, and a request mix of ½ resident-reuse, ¼ needs-load and ¼ `requires=` (tower / mac median, p99 in brackets):

| fleet | 1 req | 10 req | 100 req |
|---|---|---|---|
| 1 host / 2 dev / 3 units | 42.6 [50.5] / 13.0 | 240 [276] / 78 | 2,026 [2,121] / 713 |
| 5 hosts / 10 dev / 10 units | 146 [166] / 49 | 1,200 [1,377] / 410 | 11,489 [14,050] / 4,015 |
| 20 hosts / 40 dev / 30 units | 481 [531] / 177 | 4,034 [4,222] / 1,486 | 39,534 [43,195] / 14,505 |
| **PROD SHAPE** 2 dev / 16 units (from a real grant record), resident-reuse | **66.5 [96.4]**, run 2: 64.4 [76.2] / 22.1 | 410 [439] / 154 | — |

Cost scales roughly linearly in requests × devices. The M4 Max is 2.7–3.0× faster.

### A3. `fleet_scheduler.schedule()`

Built from `test_fleet_scheduler.py` `local()`/`spot()`/`runpod()`/`job()`, with distance and utilisation varied (tower / mac median):

| targets \ jobs | 1 | 10 | 100 |
|---|---|---|---|
| 2 | 8.6 / 2.8 | 106 / 37 | 784 / 324 |
| 10 | 12.6 / 4.7 | 348 / 119 | 2,027 / 759 |
| 50 | 35.2 / 13.1 | 2,796 / 1,062 | 16,197 [p99 16,560] / 6,202 |

### A4. `fleet_rank.rank()`

Uses the `test_preferences.py` `_node()`/`_metric()` shapes. 1 in 4 nodes is suspect and 1 in 5 is not ready. `prefer` = one attribute clause plus one `asr.quality` metric clause (tower / mac median, p99 in brackets):

| candidates | no prefer | with prefer |
|---|---|---|
| 2 | 26.1 [31.5] / 9.7 | 41.5 [48.5] / 13.8 |
| 10 | 96.1 [102.8] / 36.9 | 130.5 [144.9] / 48.0 |
| 50 | 442.6 [466.8] / 174.4 | 587.1 [610.7] / 232.9 |

`test_fleet_rank.FLEET` (3 nodes): 38.1 tower. `parse_preferences` (1 clause): 4.6.

### A5. Decision ledger

`JsonlLedger` writes to a temp dir (`/tmp` is tmpfs on the tower; writes are unsynced, so the disk-backed path is similar). The real ledgers were read only.

| op (real median-size records) | tower med / p90 / p99 | mac med |
|---|---|---|
| `Decision.to_dict()` host grant, 16 candidates | 63.5 / 67.0 / 72.7 | 27.9 |
| `JsonlLedger.append` host **grant**, 16 cands, **5,361 B** | **307** / 313 / 318 (run 2: 304) | 159 |
| `JsonlLedger.append` host **rank**, 15 cands, 7,400 B | **426** / 434 / 481 | 210 |
| `JsonlLedger.append` fleet **admit**, 14 cands, 7,774 B | **446** / 457 / 495 | 205 |
| `JsonlLedger.append` synthetic 2-candidate plan record (837 B) | 101 | 61 |
| `json.loads` of a real record (replay read side) | 35.3 (grant) · 57.0 (rank) · 58.6 (admit) | — |

**Record size:** a "typical plan decision" in production is **not** small. `_emit_plan` lists every unit on the host as a candidate, so a real host grant is ~5.4 KB. Real means, from the full census of the files on disk:

- host-broker: 4,669 B/record (grant 3.7–5.6 KB, rank 7.4 KB, observe 0.6 KB)
- fleet-broker: 7,535 B/record (admit 7.3–7.8 KB)

**Retention.** The fleet broker runs with a **64 MiB × 4** bound, not 32 × 4 (`hostd.py:1080-1086`). The census was read-only (`out/tower-A5-ledger-census.json`):

| ledger | bound | decisions that fit (at real mean) | on disk now | span retained now | at current rate |
|---|---|---|---|---|---|
| `decisions-xc-tower-ubuntu.jsonl*` (host) | 32 MiB × 4 = 134 MB | **~28.7 k** (at 837 B synthetic: 160 k) | 115.9 MB, 24,825 recs | **5.72 days** (2026-09-18 07:24 → 09-24 00:41) | current file 15.3 MB in 6.95 h ≈ 2.2 MB/h → full bound ≈ **2.5 days** |
| `fleet-decisions.jsonl*` (fleet) | 64 MiB × 4 = 268 MB | **~35.6 k** | 215.7 MB, 28,626 recs | **0.57 days (13.6 h)** | two 64 MiB files filled in 1.33 h and 1.40 h (≈48 MB/h ≈ 1.8 admits/s). At that burst rate the bound holds **~5.6 h**; at the current file's 5.5 MB/h, ~2 days |

### A6. End-to-end Harmony overhead (live, 40 requests total)

`llm_general` was resident. vLLM `/health` returned 200, and Harmony `/health` reported `resident:true`.

- **Direct backend:** `127.0.0.1:8189`, identified read-only from `ss -ltnp` (pid 523370), `/etc/harmony/llm-units.json` and the vLLM process args.
- **Request:** `max_tokens=1`, `temperature 0`, `"Say hi."`, `model=local`.
- **Order:** interleaved H,D,H,D… × 20, no warm-up. Both paths returned 200 with model `dbirks/Qwen3.8-27B-W4A16-AutoRound`.
- **Load:** tower loadavg 4.00 → 4.88.

| path | n | median ms | p90 | p99 | min | max |
|---|---|---|---|---|---|---|
| Harmony :8188 | 20 | **139.8** | 195.5 | 351.0 | 102.0 | 375.7 |
| vLLM direct :8189 | 20 | **97.1** | 139.0 | 869.8 | 81.2 | 1040.6 |
| **overhead** | | **42.7** (difference of medians) · **31.4** (median of paired differences) | 52.7 (paired p90) | | | |

**Where the overhead goes.** The ledger showed **no grant record** for these 20 requests, so the resident-reuse path does not call the host broker or `plan()`. Its parts were measured against a throwaway loopback HTTP server in the bench process, never against vLLM or Harmony:

| per-request piece in `proxy()` | tower med / p90 / p99 | mac |
|---|---|---|
| `_vllm_up()` = sync `httpx.get(.../health)`, fresh client, **called twice** on this path (`server.py:1444` and `:1475`) and blocking the event loop | **6,412** / 8,474 / 17,323 | — |
| `httpx.AsyncClient()` built and closed per request (`server.py:1657`), even though `_shared_client()` exists (`:597`, used only by `/v1/classifier`) | **5,921** / 6,274 / 7,135 | — |
| attribution: `ssl.create_default_context(cafile=certifi)` | **5,400** | 2,120 |
| `httpx.Client(verify=False)` for comparison | 241 | 108 |
| `manager._accepts` (`inspect.signature` on every `ensure()`) | 12.7 | — |
| pure policy (A1 proxy() decision) | 47–71 | 19–24 |

So about **3 × ~5.4 ms ≈ 16–19 ms of the 31–43 ms overhead is building SSL contexts for plaintext loopback hops**. The policy logic is about 0.05 ms.

## 2. Section B — Jingway weave hot path (Node, mac)

Measured with the repo's own vitest 4 runner, using a config outside the repo (`ts/vitest.bench.config.mjs`) and absolute imports of `~/jingway/src`. There is no `tsx` in the repo. Two runs: mac load 19.6 in run 1 and 3.6 in run 2; the numbers agree within ~5 %. Run 2 is shown.

| op | med / p90 / p99 µs |
|---|---|
| **B5 floor**: plain JS filter / score / choose over 10 candidates | **0.03** / 0.03 / 0.04 (inner=100) |
| B1 `weave()` 1 code step, green gate, `noRepairHost`, no sink, 79 B value | **5.67** / 6.92 / 11.59 |
| B1 `weave()` 5 steps (≈ **1.8 µs per extra step**) | 12.83 / 14.54 / 27.25 |
| B2 same, 10,007 B value | 25.38 / 28.75 / 35.25 |
| B2 same, 1,000,019 B value | **1,968** / 2,142 / 2,959 |
| B2 isolated `sha256(JSON.stringify(v))`: 79 B / 10 KB / 1 MB | 0.75 / **18.8** / **1,987** |
| B2 isolated `JSON.stringify` only: 79 B / 10 KB / 1 MB | 0.17 / 13.1 / 1,277 |
| B3 `routine({repair:'off'})` 1 step, no-op recording sink | **8.88** / 11.04 / 16.83 |
| B3 isolated `routineVersionOf(body)`: tiny / 7 KB body | 0.38 / 4.75 |
| B3 isolated `scope.child()` + `dispose()` | 0.71 |
| B4 isolated `ConversationWeaveHost.summarize()` → `conversation_messages` insert (PGLite) | **883** / 980 / 1,283 |
| B4 `weave()` via `ConversationWeaveHost`, serial | **984** / 1,061 / 1,461 → **1,016 blocks/s** |
| B4 50 concurrent blocks on **one shared host**, per-block | 23,512 / 41,729 / 48,963 (FIFO spread); batch wall 45.2 ms → **1,106 blocks/s** |
| B4 50 concurrent blocks on **50 separate hosts**, per-block | 41,273 / 42,794 / 43,577; batch wall 43.0 ms → **1,163 blocks/s** |

PGLite boot plus migrations took 645 ms. The bench wrote 4,400 messages.

**Caveat:** PGLite is single-connection in-process WASM Postgres, so the database serialises every insert (~0.9 ms each). The per-host write queue therefore shows up only as a FIFO spread of per-block latency. It cannot show up as lost throughput here, because shared and separate hosts both hit the same ~1.1 k blocks/s ceiling. With a pooled real Postgres, separate hosts would parallelise and the shared host would stay serial. This bench cannot show that gap.

**Gate hashing.** Per-gate hashing is ~0 at 100 B, **~75 % of the block at 10 KB** and **~100 % at 1 MB**.

## 3. Section C — native boundary cost

| op | tower | mac |
|---|---|---|
| **PyO3, livestack `shared_py.Planner`** (the production wheel, already the Rust decision core behind `livestack_node.manager`) | | |
| `Planner.known(name)` → bool | **0.09** (inner=200) | — |
| pure Python `name in dict` | 0.05 | — |
| `Planner.resident()` → Vec<String>(3) vs Python `sorted(set)` | 0.22 vs 0.20 | — |
| `Planner.plan_acquire(False, name)` → (Vec, Vec) | 0.39 | — |
| `Planner(16 unit tuples)` (marshal in) | 1.85 | — |
| **PyO3, jingway `python-bridge`** (built with maturin, abi3) | | |
| `TreeState.node_count` getter vs `len(dict)` | — | 0.03 vs 0.02 |
| `line_similarity(str, str)` → f64 | — | 0.26 |
| `TreeState.compute_hashes()` 10 nodes vs **the same algorithm in pure Python** | — | **1.40 vs 17.05 (12×)** |
| `TreeState.from_json(1,894 B)` vs `json.loads` | — | 4.33 vs 5.95 |
| **WASM, jingway `wasm-bridge` nodejs pkg** (prebuilt, 2026-09-08) | | |
| `lineSimilarity(str, str)` | — | 0.17 (JS `===` floor 0.005) |
| `requestHash(182 B object)` vs verbatim JS `hashJson(normalize())` | — | **7.50 vs 2.19 (WASM 3.4× slower)** |
| `requestHash(223 B object)` vs JS | — | 6.89 vs 1.85 |

**Summary of the boundary:**

- **PyO3 crossing costs about 40–80 ns per call.** That is negligible, and the 50 µs control shows the harness would have seen more.
- **Marshalling structured data** costs ~2 µs for 16 tuples. JSON strings in cost less than Python's own `json.loads`.
- **Pure-Rust compute** on string-heavy work is ~10× faster.
- **WASM with JS-object (serde-wasm-bindgen) arguments loses to V8** on small canonical-JSON-plus-sha256 work, because Node's `crypto` is already native.

A side observation, not acted on: `core/crates/core/tests/fixtures/request_hash_expected.json` holds `c712a0a6…` for `multi-op-shuffled-keys`. Both the current verbatim JS oracle and the WASM build produce `30e21f0a…`. The committed fixture has drifted from its own generator.

## 4. What could NOT be measured, and why

- **`bench_proxy_parts.py` on the mac:** `livestack_node.manager` hard-requires the compiled `shared_py`, which is not built on the mac. It ran on the tower only.
- **Harmony admission latency end to end** (the non-resident path, then `admit()`, then host broker HTTP, then `plan()`, then the ledger, then the grant): not measured live. Doing so would change residency or state on a production host. Only its pure CPU parts were measured (A2, A5).
- **Head-of-line blocking on real Postgres** for `ConversationWeaveHost`: PGLite serialises everything (see §2).
- **Node/TS on the tower:** deliberately not run (project rule). §2 and the WASM part of §3 are mac-only.
- **A faithful offline replay:** real ledger records carry candidate ids, priorities, tiers and a residency flag, plus measured-free inside a reason string. They do not carry unit footprints or device capacities. `plan()` cannot be re-run from the ledger alone, so replay throughput below is computed from its parts (record parse plus `plan()` at prod shape), not measured on real replays.
- **A Rust `plan()`:** none exists, so there is no candidateResult. The 10–12× figure is extrapolated from the jingway `compute_hashes` pair and is not a measurement of `plan()`.

## 5. What this means

### Where time goes, per routing decision (tower, production hardware)

| path | total | policy CPU | largest items |
|---|---|---|---|
| **(i) resident-reuse** (almost every Harmony request) | Harmony overhead ≈ **31–43 ms** on a 140 ms, 1-token request | request matching **47–71 µs**, of which ~27 µs is the adapter file re-read; `plan()` is not called | 3 fresh SSL contexts ≈ **16–19 ms**; the rest is FastAPI/httpx streaming proxy and scheduling on a load-4 host |
| **(ii) warm admission** (host broker grant, no load) | ≥ HTTP round trip + broker CPU | `plan()` **65 µs** at prod shape | **ledger append 305 µs (host grant) + 440 µs (fleet admit)** = 5–7× the planner. `rank()` over 10 candidates adds 96–131 µs. A cold admission adds a **50–135 s** model load |
| **(iii) offline replay** (per decision) | ≈ **100–125 µs** → **~8–10 k decisions/s per tower core** (~3× on the M4) | `plan()` 65 µs ≈ **55–65 %** | `json.loads` of the 5–8 KB record, 35–58 µs |

| per weave block (mac) | cost |
|---|---|
| bare `weave()` | **5.7 µs** + 1.8 µs per extra step |
| `routine({repair:'off'})` | **8.9 µs** |
| sha256 gate fingerprint | 0.75 µs at 100 B · 19 µs at 10 KB · 2 ms at 1 MB |
| `ConversationWeaveHost` persistence | **~0.9–1 ms** per block |
| the policy decision itself in JS (10 candidates) | **0.03 µs** |

### Would a Rust/PyO3 policy evaluator be MATERIAL (>10 % of end-to-end decision latency)?

1. **Resident-reuse: NO (~0.1–0.2 %).** Even a perfect evaluator that removes all ~50–70 µs saves under 0.2 % of the 31–43 ms overhead, and ~0.04 % of the request. Two Python-level fixes are each worth 100–300× more:
   - reuse one HTTP client, including for `_vllm_up`, which saves ~16–19 ms;
   - cache `_attributes_for`/`_adapters_for`, which saves ~27 µs per unit checked.
2. **Admission: NO at today's fleet** (1 host, 2 GPUs, 16 units, 1–10 pending). `plan()` is 65 µs against a ≥ ms HTTP admit plus 0.3–0.45 ms of ledger serialisation per record, and a model load of seconds to minutes when one is needed. The ledger emit costs **5–7×** the decision it records, so it is the bigger CPU item. It becomes material only at larger scale:
   - `plan()` reaches 1.2 ms at 5 hosts / 10 devices / 10 pending, and **4 ms (10 pending) to 40 ms (100 pending) at 20 hosts / 40 devices**;
   - `schedule()` reaches 16 ms at 50 targets × 100 jobs.
3. **Offline replay throughput: YES (~55–65 % is `plan()`).** A ~10× faster evaluator would give ~2–2.5× replay throughput. Moving record parsing into Rust (serde) too could approach ~10× (~80–100 k decisions/s per core). But in absolute terms, the whole retained ledger (~25 k host plus ~29 k fleet decisions) already replays in **~3–7 s on one tower core**. The win matters for **policy sweeps** (N variants × 25 k decisions: 100 variants ≈ 4–5 min in Python vs ~30 s in Rust). The binding constraint is state, not speed: records lack footprints and capacities, so the ledger cannot drive `plan()` yet.

### For the Jingway hot-policy-tier design

**Keep weave out of the per-request loop.** Wrapping each routing decision in a `weave()`/`routine()` block adds 6–9 µs per decision. That is 200–300× a JS policy evaluation, but still under Python's 50–70 µs. Adding `ConversationWeaveHost` persistence adds **~1 ms per decision**, which is **15–20× the entire current Python policy cost** and caps at ~1.1 k blocks/s on one PGLite. A hot tier should evaluate policy on the bare path and persist summaries asynchronously or in batches.

**Pick the native boundary by shape:**
- PyO3 or napi with **string/JSON or scalar arguments** is cheap (tens of ns).
- **Structured JS objects through WASM/serde** cost more than they save for small inputs.

**Noise on the tower.** loadavg ranged 1.7–4.9 during the Python runs and spiked to 9.9 during the loopback-HTTP run. Repeat runs of the key rows agreed within 3 %, with p99/median mostly ≤ 1.3×. The live A6 probe (n = 20 each) is the noisiest number here: the direct path had a 1.04 s outlier. Treat the 31–43 ms overhead as ±10 ms.

## 6. Reproduce

All scripts are under `<jingway>/openspec/changes/compiled-policy-routines/receipts/bench/`, and mirrored to `xc-mac-studio:/tmp/jingway-hot-bench/`.

```bash
B=<jingway>/openspec/changes/compiled-policy-routines/receipts/bench
bash $B/run_tower.sh                    # tower: A1-A6, shared_py, census (A6 only if resident; 40 reqs)
rsync -a $B/py $B/ts $B/fixtures $B/run_mac.sh 100.64.0.2:/tmp/jingway-hot-bench/
ssh 100.64.0.2 bash /tmp/jingway-hot-bench/run_mac.sh   # mac: A1-A5 comparison, B, C
```

| script | measures |
|---|---|
| `py/harness.py` | timing harness and positive controls |
| `py/bench_server.py` | A1: AST extraction from server.py |
| `py/bench_policy.py` | A2, A3, A4 and synthetic A5 |
| `py/bench_realshape.py` | prod-shape `plan()` and real-record ledger appends |
| `py/ledger_census.py` | A5 retention census (read-only) |
| `py/bench_replay_parse.py` | replay read side |
| `py/bench_e2e.py` | A6 live probe, 40 requests |
| `py/bench_proxy_parts.py`, `py/bench_ssl_ctx.py` | A6 attribution |
| `py/bench_shared_py.py` | C, livestack PyO3 |
| `py/bench_jingway_py.py` | C, jingway PyO3 |
| `ts/weave.bench.test.ts` + `ts/vitest.bench.config.mjs` | B |
| `ts/bench_wasm.mjs` | C, WASM |

Outputs are in `out/` (tower) and `out/mac/` (mac, copied back): `*.log` plus per-row `*.json`.

- **Mac leftovers:** `/tmp/jingway-hot-bench/{venv312,target (136 MB),wheels}` are throwaway and safe to delete.
- **Tower leftovers:** `bench/tmp/` holds the scratch ledgers written by the A5 bench.

## 7. Admit traffic (task 0.1)

Measured 2026-09-24 ~18:05 UTC, **read-only**: one Python pass over
`~/.cache/livestack/fleet-decisions.jsonl*` on xc-tower-ubuntu (4 files, 235 MB). No service
was touched. Scripts: `census.py`/`census2.py`/`census3.py` in the agent scratchpad (not
committed; they are ~40-line group-bys over `ts`, `request.owner`, `request.principal`,
`kind`, `outcome.status` and `reason`).

**Window:** 29,076 admit records over **25.67 h** (2026-09-23 16:xx → 2026-09-24 18:0x UTC).
Other decisions in the same files: observe 619, load 599, evict 582, rank 2. Every admit
is `sla=batch`.

| rate | all admits | admits that got a lease (`outcome.status=ok`, 22,711) |
|---|---|---|
| average | **1,133 /h (0.31 /s)** | **885 /h (0.25 /s)** |
| peak hour | 5,890 (09-23 17 UTC, 1.64 /s) | 4,139 |
| peak minute | 1,304 | 363 |
| peak second | **195** | 110 |

Quiet hours (09-23 19 UTC onward) run 400–1,200 admits/h, almost all granted.

**Callers.** Three owners and two principals account for everything:

| owner | principal | kind | admits |
|---|---|---|---|
| `attune:acct_c082baa1-…` | `attune` | llm | 24,843 |
| `attune:corpus` | `attune` | llm | 3,709 |
| `attune:acct_c082baa1-…` | `attune-worker` | polytts | 174 |
| `attune:acct_c082baa1-…` | `attune-worker` | polyasr | 169 |
| `attune:acct_c082baa1-…` | `attune-worker` | llm | 162 |
| `attune:probe` | `attune-worker` | polytts/polyasr/llm | 19 |

The `attune-worker` traffic is steady (median gap ~445 s per owner/kind). All the volume is
principal `attune`, kind `llm`.

**Refusals.** 6,365 admits (21.9 %) got no lease (`outcome` = `{}`):
- `no llm target in na: …` (region policy `allow:[na]` rejects every target): 4,298
- `refused: account quota: … holds 8 of 8 slot(s)`: acct 1,263, `attune:corpus` 796
- `no polytts target in na`: 8

### Verdict: **a retry loop is present.**

By the task's test (the same owner/kind repeating within seconds with no lease), the
refusals come almost entirely in tight loops:
- **42 episodes** of ≥ 10 consecutive refusals for one owner/kind/reason with < 5 s between
  admits. They hold **5,403 admits**, 85 % of all refusals and 18.6 % of all admits.
- Each retry carries a fresh `job_id` (`llm-<ms>`, 29,024 distinct ids for 29,076 admits),
  so this is the client re-admitting, not the broker replaying.
- It has no backoff. The inter-admit gap is ~5 ms, and episodes run at **45–190 admits/s**.
  Largest episodes:
  - acct, `no llm target in na`, 09-23 18:32:44 UTC: 690 admits in 7.1 s (98 /s)
  - `attune:corpus`, quota full, 09-23 16:29:05 UTC: 362 admits in 1.9 s (189 /s)
- Most of the volume came on 09-23 between 16 and 18 UTC. That window is the "~1.8
  admits/s" burst the earlier section measured: 30–63 % of each of those hours was refusals.
  Smaller loops recur through the whole window, e.g. 160 refusals at 02 UTC and 100–110
  at 04 UTC and 07 UTC.

**What it does to this change's estimates:**
- The refused admits are not policy exposures worth tuning on. Each one is a decision with
  no feasible target, or one refused by quota, and repeated hundreds of times per second.
- If they were recorded, they would dominate the record stream's size during bursts, and
  so the task 3.3 sizing.
- Granted admits look like genuine traffic: ~885 /h average, one principal, fresh job ids,
  no refusal in between. They still peak at 110 /s, which with an 8-slot quota means leases
  far shorter than one second.

For sizing (task 3.3), use the **granted** rate: 885 /h average (21.2 k/day). The all-admit
rate is 1,133 /h, and it includes the loop.

**Stopped here, per task 0.1:** the operator decides what happens to the attune retry
behaviour (client-side backoff on `refused`/`no target`, or broker-side rate limiting)
before this change continues.
