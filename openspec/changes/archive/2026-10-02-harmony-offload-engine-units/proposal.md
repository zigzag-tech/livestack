## Why

The fleet wants to serve **Qwen3.8-Flash-Next** (Qwen's 125B mixture-of-experts model,
24,576 experts, ~10 active per token) through Harmony: a caller asks for it with
`require:` clauses like any other LLM, and Harmony makes room, loads it and serves.
Today Harmony cannot do that, and nothing about the model is the reason. The limits
are all in Harmony:

1. **harmony-llm can only start vLLM.** `node-py/examples/harmony-llm/server.py`
   `_load()` hard-codes `venv/bin/vllm serve <model> …`, readiness is `_vllm_up()`, and
   the footprint comes from parsing vLLM's own startup log (`StartupCapture`,
   `_record_measurement`). vLLM cannot run a 125B model on a 24 GB card: even at
   4 bits the weights are ~60–65 GB, and vLLM has no efficient way to keep most of
   them in host RAM.
2. **The only engine that can run it on one 3090 is Strata**
   (<https://github.com/architectds/Strata>; upstream README points at
   `Niko1221/Strata`). It is a custom C++/CUDA engine, partly derived from llama.cpp,
   with an OpenAI- and Anthropic-compatible HTTP server (`serve/server.py`). It keeps
   the hottest experts on the GPU, **pins all of them in host RAM (35–55 GB depending
   on the quant)**, computes the rest on the CPU, and reads a lookup table from SSD.
   Its cost is therefore mostly **host RAM**, a dimension the GPU planner does not
   charge today (planner `Device.capacity` is `{"vram_bytes": …}` only;
   `hostbroker.py` builds every device and unit footprint from `vram_bytes`).
3. **Strata wants the whole card.** Its server refuses to start when the GPU is in
   use (`GpuBusy` in `serve/server.py`; `--min-free-vram-mib` sets the threshold) and
   sizes its expert cache to whatever VRAM is free. A footprint cannot express
   "everything on this device".
4. **Strata serves one request at a time** (`serve/server.py` serialises every request
   on one lock: `self.fifo`; docs: "one request at a time"). A vLLM unit serves up
   to `--max-num-seqs` at once (`llm_general`: 32). Nothing in a unit's attributes
   tells a caller or the planner which kind it is getting.
5. **Loads are slow and violent.** Strata's first start pins 35–55 GB and can make
   the host unresponsive for 1–3 minutes (its own README). Every eviction and reload
   pays that again. The anti-thrash guards (`min_residency_s`, `reload_cost`) exist,
   but the unit has to declare and measure them honestly for the planner to weigh
   them.

The user's stated goal (2026-10-02): **"The whole point of this exercise is to make
Harmony support things like that."** The deliverable is not a Strata install; it is
Harmony learning to arbitrate a non-vLLM, host-RAM-heavy, whole-card,
single-stream engine. Strata on Qwen3.8-Flash-Next is the first instance and the
acceptance test.

### Where it can run (fleet survey, 2026-10-02)

| Host | GPU | Host RAM | Verdict |
|---|---|---|---|
| xc-tower-ubuntu (100.64.0.18, Vaughan) | 2× RTX 3090 24 GB | 94 GB (~64 GB available) | **Only viable host.** Card 1 (`xc-tower-ubuntu/a46c4c2e`) holds only `llm_general` (UNPINNED, 22.5 GB). Card 0 (`…/4bac2869`) holds HARD_PIN `asr` + others |
| zz-tower0 (100.64.0.3, Nanjing) | 1× RTX 3090 | **29 GB** | Below Strata's 32 GB floor. Best CPU for it (Ryzen 5 7600, AVX-512) — revisit if RAM is upgraded to ≥64 GB |
| zz-joe (100.64.0.24) | 2× RTX 2070 8 GB | 31 GB | Below RAM floor, 8 GB cards |
| xc-mac-studio | Apple M4 Max | 36 GB unified | Strata has no Metal backend |

Consequence: the first deployment target is **xc-tower-ubuntu, card 1**, where the
only tenant is UNPINNED and the planner may legally evict it. No residency policy has
to change for the acceptance test (design §5 records why card 0 was rejected).

## What Changes

1. **Engine adapters in harmony-llm.** A unit spec gains `"engine"` (`"vllm"` default,
   `"strata"` new). Each engine supplies: argv builder, readiness probe, a free/stop
   routine, a startup-measurement parser, and the attributes it can derive from its
   own launch line. The vLLM path is moved behind the same interface without
   behaviour change. Engine adapters live in harmony-llm (an example node), not in
   `livestack_node`: the planner stays engine-agnostic.
2. **Host RAM as a planned resource.** Units may declare and report
   `ram_bytes` (pinned/locked host memory) in their footprint. The GPU planner gains a
   **host pool**: one capacity vector per host (`ram_bytes` from measured
   `MemAvailable` minus a reserve, via `hostview.py` from `host-memory-ledger`), and a
   placement fits only if it fits on the device **and** on its host pool. Eviction
   may free RAM as well as VRAM. This realises the `ram_bytes` dimension
   `_plans/resource-planner.md` §2 has promised since it was written.
3. **Whole-device footprint.** A unit may declare `"exclusive_device": true`. The
   planner charges it the device's entire capacity (so every other tenant on that
   device must be evictable, or the request is refused with a reason naming the
   pinned tenant). The granted budget then reaches the engine as "all of it".
4. **Concurrency as an attribute.** Every LLM unit advertises `max_concurrent`
   (vLLM: from `--max-num-seqs`, default 256 when unset — vLLM's own default; Strata:
   1). Derived from the launch line, never hand-declared, so it cannot lie. Callers can
   require it (`max_concurrent=[8,]`), and harmony-llm queues rather than forwards
   when a unit is at its limit, reporting the queue in `/residence`.
5. **Pinned engine source.** An engine unit names its build: `engine_source:
   {"repo": "<git url>", "rev": "<full sha>"}` plus the build/model artefacts it uses.
   harmony-llm refuses to start a unit whose installed engine does not match the
   pinned rev and reports the mismatch. The user requires a **specific fork version**
   of Strata (open question Q1 in design.md: which fork, which rev).
6. **Acceptance deployment — driven only by the request language.** A `flash_next`
   unit on xc-tower-ubuntu card 1 serving Qwen3.8-Flash-Next through Strata,
   UNPINNED, **not** `warm_on_start`, **not** `default`, lower priority than
   `llm_general`. It becomes resident for exactly one reason: a **broad** request
   stating characteristics only it meets, e.g. long context
   (`{"model": "require:class=llm,context_len=[131072,]"}`) or "a big model"
   (`params_b=[60,]`) — never a clause that names it in disguise. Harmony chooses
   between Strata and the 27B from the characteristics (design §4b), and LLM
   `prefer` clauses ("most capable", "fastest") join the vocabulary. **Nobody force-loads
   it** — no `POST /load` on Strata, no `/model/warm`, no `systemctl start` of the
   engine, no hand-edit-and-restart. Callers never name an engine, a unit, a card or a
   host; `engine` is not a requestable attribute. If a request that should reach
   Flash-Next cannot be written in the request language, that is a defect in the
   language to fix in this change, not something to route around. The full scenario
   list is in the delta spec.
7. **Context length decides, without the caller restating it.** Explicit
   `require:class=llm,context_len=[N,]` already works (verified live; today it
   correctly answers "nothing satisfies" above 24,576). New: when an engine refuses
   an un-named request as too long, harmony-llm derives `context_len>=<measured
   need>` and routes it once more (design §4c), so a long prompt reaches Flash-Next
   without a 413-and-resubmit; `prefer llm.context_len: max` covers "the widest you
   have".
8. **Two adjacent defects fixed on the way** (they would make the acceptance test lie):
   - `GET /fleet/rank?kind=llm` reportedly ignores `require=` for LLM nodes (reported
     2026-10-02 by a survey agent; verify first, design §9). A Strata node that is
     warm must never become the hub's fallback for plain `llm` traffic.
   - zz-tower0's `~/livestack` is 67 commits behind origin while its hostd runs from
     it. Not in scope to fix here, but the deploy task must not assume fleet brokers
     run the same code (design §8).

## Capabilities

### New Capabilities
- `harmony-engine-units`: units served by an engine other than vLLM, host-RAM
  footprints and the host pool, whole-device units, advertised concurrency, pinned
  engine builds.

### Modified Capabilities
- None by delta. `unit-measured-cost` still holds for vLLM units; its
  `source: "vllm-startup"` gains a sibling `source: "strata-startup"` described in the
  new capability rather than by editing the existing requirement.

## Impact

- **Code:** `node-py/examples/harmony-llm/server.py` (2,051 lines — over the 1,500-line
  bound benchday uses; do not grow it: put engines in a new `engines/` package beside
  it), `node-py/livestack_node/planner.py` (host pool in `WorldState`, `_fits`),
  `hostbroker.py` (assemble host pool, carry `ram_bytes`/`exclusive_device` from node
  reports), `manager.py` (`ManagedUnit.footprint` becomes a vector, int still accepted),
  `fleet_rank.py` (requirement filter, if §9 confirms the defect), `hostview.py`
  (reused, not changed, unless a gap is found).
- **Docs:** `HARMONY.md` (new section "Engines other than vLLM"; host pool in
  "Context-awareness"), `_plans/resource-planner.md` §2 (stale line about `ram_bytes`
  being future), harmony-llm `README.md` (engine field, Strata unit example),
  `~/.claude/docs/mesh.md` xc-tower-ubuntu notes (host state not in any repo).
- **Hosts:** xc-tower-ubuntu only. Needs ~80 GB disk for the model + MTP layer; the
  root disk had 81 GB free on 2026-10-02 (96% used) — run `diskreap` first.
  `/etc/harmony/llm-units.json` gains a second unit.
- **Callers:** none change. Plain `model: "local"` and every existing `require:` that
  matches `llm_general` keep routing to `llm_general`. Only callers who ask for
  Flash-Next's attributes reach it.
- **Design records realised:** `_plans/resource-planner.md` (multi-resource
  footprints, §2). **Stale in it:** §2 lists `ram_bytes` as "multi-resource" future
  work; `host-memory-ledger` design §6 explicitly deferred "a `host` input to the GPU
  planner's world (pure `plan()`)" — this change is that later change.
