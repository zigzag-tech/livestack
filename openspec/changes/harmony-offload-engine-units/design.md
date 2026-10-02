# Design — harmony-offload-engine-units

Read `proposal.md` first. This file is written for an agent with **no context from
the conversation that produced it**. Everything the work depends on is stated here
or named by path. When this file and the code disagree, the code is the truth:
stop and correct this file in the same commit.

## 0. Ground rules (from the user, 2026-10-02 — non-negotiable)

1. **Never force-load.** Strata becomes resident only because a request, written in
   Harmony's request language, needs a unit that only Strata satisfies. Not allowed,
   ever, including "just to test": `POST /load` on Strata's server, Harmony
   `/model/warm`, `systemctl start` of an engine process, `warm_on_start: true`,
   `default: true`, `residency: HARD_PIN/SOFT_PIN` for this unit, or a hand edit of
   `/etc/harmony/llm-units.json` plus a restart to make a request route somewhere.
   The request language reference is benchday `docs/livestack-harmony.md` (on
   xc-tower-ubuntu: `~/benchday/docs/livestack-harmony.md`).
2. **If a request cannot be expressed, fix the language.** "Hand-editing
   `llm-units.json` and restarting `harmony-llm` to change an inference parameter is
   working around Harmony, not using it — and that is a bug in the request language."
3. **Callers never name an engine, unit, card or host.** `engine` is an
   implementation fact of a unit, not a requestable attribute. A caller wanting
   Flash-Next asks for what it IS (`class=llm, family=qwen, params_b=[100,]`, and if
   needed `arch=moe`), never for "strata" or "flash_next". Requests should be **as
   broad as possible**: state characteristics (long context, big, fast,
   concurrent) and let Harmony choose between Strata and the 27B — §4b.
4. **Pin the engine to a specific fork and revision.** The user requires a specific
   fork version of Strata. Which one is open question **Q1**: do not guess, do not
   use `main` of whatever is cloned. Until Q1 is answered, tasks 1–4 (engine-agnostic
   work) may proceed; task 5 (installing Strata) may not.
5. **Absence and failure must not look alike** (livestack config.yaml). An
   unmeasured RAM footprint is `unknown`, never 0; a refused placement names the rule
   and the tenant that refused it.

## 1. What exists today (verified 2026-10-02 against origin/main `e14ff6ca`)

### The request path for an LLM

1. A caller sends an OpenAI request to a **harmony-llm node**
   (`node-py/examples/harmony-llm/server.py`). On xc-tower-ubuntu the card-1 node is
   `tower-llm`, device `xc-tower-ubuntu/a46c4c2e`, serving unit `llm_general`
   (`dbirks/Qwen3.8-27B-W4A16-AutoRound`, vLLM port 8189) from
   `/etc/harmony/llm-units.json`.
2. `_requirement_from()` parses `harmony_requires` or `model: "require:…"`;
   `_derived_requirements()` adds `vision` / `thinking` / `tools` / `class`
   derived from the body (benchday doc, "Derived, not declared").
3. `_local_satisfies(name, requires)` filters the node's units by their
   `attributes` (built by `_attributes_for(spec)`, which derives `thinking`,
   `tools`, pooling and vision facts from the vLLM launch line).
4. The chosen unit's `ManagedUnit` (`livestack_node/manager.py`) is ensured: it
   asks its host broker (`hostd.py` → `hostbroker.py`, port 8799) to admit it; the
   pure planner (`planner.py`, `plan()`) decides what to evict; the broker dispatches
   the evictions, then calls the unit's `loader(device=…, budget=…)`.
5. `loader` is `_load(name, device, budget)`: builds `vllm serve …` argv, sizes
   `--gpu-memory-utilization` from the planner's `budget["vram_bytes"]`, starts the
   subprocess in its own session, waits for `_vllm_up()`, then
   `_record_measurement()` parses vLLM's startup log into the measured cost
   (`unit-measured-cost` spec). `freer` is `_free(name)`: SIGTERM the process group,
   wait, SIGKILL after 60 s.
6. The request is proxied (`_proxy_impl`) to the unit's local port.

### What the planner can and cannot express

- `Unit.footprint` / `Device.capacity` are resource **vectors** (`Res`), and
  `_fits(need, avail)` already checks every dimension. But every producer only fills
  `vram_bytes`: `hostbroker.py` builds `Device(capacity={"vram_bytes": …})`
  (~line 328) and unit footprints from `vram_bytes` (~lines 1750–1775);
  `ManagedUnit.footprint` is an `int` of bytes (`manager.py` ~line 100).
- There is **no host-scoped resource**. Several devices on one host share host RAM,
  so `ram_bytes` cannot simply be a per-device dimension: two Strata units on two
  cards of one host would each fit their card and together overcommit the host.
- `Residency`: `HARD_PIN=0` (never preempted, `min_resident` fleet floor),
  `SOFT_PIN=1` (preemptible, restored with debounce), `UNPINNED=2`.
- Preemption is **idle-only** by default (`allow_busy_preemption` off): a unit with
  an active lease is never evicted mid-request.
- `spread_group`: units in the same group (`"llm"` for every harmony-llm unit) are
  alternatives; `_contention_cost` makes the planner avoid co-placing siblings that
  are demanded in alternation.

### Measured host state on the target (xc-tower-ubuntu, 2026-10-02)

- CPU: Intel i5-9400, 6 cores, AVX2 only (no AVX-512). Strata computes non-resident
  experts on the CPU; expect decode below Strata's own 3090 estimate (100–140 tok/s,
  measured on a Ryzen 5 7600 with AVX-512). Unmeasured here.
- RAM: 94 GB total, ~64 GB available, **~55 GB swap in use** across three swap
  files. Pinned memory cannot be swapped, so Strata's pin pushes other tenants out.
- Disk: `/` 1.8 TB, **81 GB free (96%)**. Strata needs ~70–80 GB for the model plus
  ~6 GB for the MTP draft layer. Run `diskreap scan` / `diskreap clean` (SAFE
  categories only) first. Never `du`/`find` over `$HOME` or `/` (network mounts hang).
- Card 0 `xc-tower-ubuntu/4bac2869`: `asr` **HARD_PIN** 8 GB resident; `embed_multi`
  3.2 GB UNPINNED resident; TTS / align / diarize / OCR / perception units UNPINNED,
  loaded on demand.
- Card 1 `xc-tower-ubuntu/a46c4c2e`: `llm_general` UNPINNED, 22.5 GB declared,
  resident. Nothing else.
- Rule 15 of benchday's `AGENTS.md`: heavy builds/tests do not run on this tower.
  Building Strata's CUDA engine is a heavy build — build it on zz-joe or
  xc-mac-studio? No: the binary must match this host's driver/arch (sm_86, CUDA
  13.0). Use Strata's prebuilt release/Docker artefact for the pinned rev if one
  exists; otherwise build here under `nice -n 19` and say so in the landing message.

### Strata facts the adapter depends on (from its repo at `36fa455`, 2026-10-02)

- Install: `./setup.sh --yes --family qwen --model <SIZE> --no-start` (non-interactive;
  `docs/AI_SETUP.md`). Sizes: `Q2_0` (37.6 GB RAM+VRAM, fastest), `IQ2_XS`,
  `IQ3_XXS`, `IQ3_S` (best, slowest), `Coder` (code-only expert-pruned, fits 32 GB).
- Server: `serve/server.py` with `--engine strata --config <json> --port N --host
  127.0.0.1 --gpu <idx>`; options that matter: `--lazy` (start unloaded),
  `--idle-unload SECONDS`, `--min-free-vram-mib`, `--before-load CMD`, `--api-key`.
- Endpoints: `/health`, `/status` (`busy`, `queued`, phase), `/slots` (single slot),
  `/v1/models`, `/v1/chat/completions`, `/v1/messages`, `POST /load`, `POST /unload`.
- `GpuBusy` is raised when free VRAM is below what the model needs.
- One request at a time (`self.fifo` lock). Optional conversation parking
  (`--conversation-cache-mib/--conversation-cache-slots`) reuses prefixes but never
  runs requests concurrently.
- Reasoning effort: off/low/medium/high via the OpenAI "reasoning effort" field.
  **Unverified:** whether reasoning is returned in a separate field or inline in
  `content`. This decides the `thinking` capability (§4).

## 2. Engine adapters (harmony-llm)

Ownership: harmony-llm (the node process) owns engine subprocesses and their
measurements. `livestack_node` never learns engine names.

New package `node-py/examples/harmony-llm/engines/` with one module per engine and a
small protocol:

```python
class Engine(Protocol):
    name: str                                    # "vllm" | "strata"
    def argv(self, spec, port, budget) -> list[str]
    def env(self, spec, base_env) -> dict
    def ready(self, spec, port) -> bool          # cheap, called every 2 s
    def measure(self, spec, capture, pid) -> dict | None   # unit-measured-cost shape
    def launch_attributes(self, spec) -> dict    # derived: thinking/tools/vision/max_concurrent
    def stop(self, proc) -> None                 # default: _free's SIGTERM→wait→SIGKILL
```

- `vllm.py` is the existing code moved, **byte-for-byte behaviour** (argv incl.
  `--scheduling-policy priority`, LoRA args, budget→`--gpu-memory-utilization`,
  `StartupCapture` measurement). The existing harmony-llm tests are the
  regression gate.
- `strata.py`:
  - argv: `<strata_root>/.venv/bin/python serve/server.py --engine strata --config
    <cfg> --host 127.0.0.1 --port <unit port>` (+ `--gpu` only if the node does NOT
    already set `CUDA_VISIBLE_DEVICES`; it does — see `_load`'s comment on metering;
    with one visible card Strata sees index 0). **Never** pass `--idle-unload`,
    `--lazy` or `--before-load`: idle eviction and loading are Harmony's job, and a
    second idle timer inside the engine would free memory the planner still thinks
    is held (or the reverse).
  - Budget: Strata sizes its expert cache from free VRAM itself. With
    `exclusive_device` (§4) the grant is the whole device, so no translation is
    needed; record the granted bytes in the start log line as vLLM does.
  - ready: `GET /health` 200 **and** `/status` reports the model loaded (not merely
    the HTTP server up — the server answers before the engine says READY;
    `serve/server.py` #344 comment). Confirm the exact field on the pinned rev.
  - measure: VRAM from NVML for the engine pid; RAM from `/proc/<pid>/status`
    (`VmLck`, `VmPin`) and the cgroup's `memory.current`. **Verify which field
    carries cudaHostRegister-pinned memory with a positive control** (start the
    engine, compare the field before/after READY; a field that reads 0 is not
    "nothing pinned"). Also parse Strata's own log line reporting expert slots and
    GiB per card. `source: "strata-startup"`.
  - Start timeout: Strata loads 35–55 GB and can take 1–3 min, longer on the first
    start. Use a per-engine timeout (default 900 s, as vLLM).
- `_load`/`_free` become thin: pick the engine from `spec["engine"]`
  (default `"vllm"`), call it. `_vllm_up` → `engine.ready`. Keep the start-failure
  cooldown and the foreign-listener check for all engines.
- `server.py` is 2,051 lines; **do not add net lines to it**. Moving vLLM code out
  should make it shrink.

## 3. Host RAM in the planner (the host pool)

Ownership: the **host broker** (`hostbroker.py`) owns the host pool's capacity
(from `hostview.py` measurements it already reads for `host_mem`). Nodes own their
units' measured `ram_bytes`. The planner remains a pure function.

- `WorldState` gains `hosts: Mapping[host_id, Res]` (free host-scoped capacity,
  e.g. `{"ram_bytes": …}`) and `Device.host_id` already exists.
- `Unit.footprint` may carry `ram_bytes`. In `plan()`, a placement fits iff
  `_fits(device_dims(need), device_free)` **and**
  `_fits(host_dims(need), host_free[device.host_id])`. Dimension routing is a fixed
  set: `HOST_DIMS = {"ram_bytes"}`; everything else is device-scoped. Evicting a unit
  returns its `ram_bytes` to the host pool.
- Host free = `MemAvailable − reserve − Σ (claims of resident units not yet
  reflected in MemAvailable)`. Reuse `host-memory-ledger`'s arithmetic and reserve
  rather than inventing a second one; read its design §3 before writing this.
- **Unknown host capacity is not infinite.** If the broker has no host measurement,
  a unit with `ram_bytes` is refused with reason `host memory unmeasured`; units
  without `ram_bytes` are unaffected (zero behaviour change for every existing unit).
- Workload placement (`workloads/placement.py`, from host-memory-ledger) must see a
  resident Strata's memory. It already charges model servers by learned cgroup peak
  if they are listed in `host_services`; add harmony-llm's systemd unit on
  xc-tower-ubuntu to that list in the deploy task and verify the learned peak
  reflects Strata's pin.
- This is the "`host` input to the GPU planner's world (pure `plan()`)" that
  host-memory-ledger design §6 named as the seam for a later change. It does not
  gate loads on PSI pressure; that remains rejected for the reason given there.

## 4. Unit declaration: whole device, concurrency, capability honesty

- `exclusive_device: true` → the planner charges the device's full capacity on every
  device dimension. Planner refusal reason when a non-evictable tenant blocks it:
  `exclusive unit flash_next needs device a46c4c2e empty; asr is HARD_PIN there`.
- `max_concurrent`: derived by `Engine.launch_attributes` (vLLM: `--max-num-seqs`,
  else vLLM's default for the installed version — read it from the engine, do not
  hard-code; Strata: 1). Published in `attributes`, so the existing requirement
  grammar can filter on it (`max_concurrent=[8,]`). harmony-llm holds requests beyond
  `max_concurrent` in a bounded FIFO per unit (bound: 64 waiting; beyond that 429
  with a reason) instead of forwarding them to an engine that would serialise them
  invisibly. The queue depth is reported on `/residence`.
- Capability attributes for `flash_next` follow the capability/parameter rule in
  `docs/livestack-harmony.md`: `thinking: true` **only if** Strata returns reasoning
  separately from `content`; otherwise `thinking: false`, so a request setting
  `enable_thinking` is not routed to a unit that would leak narration into the
  reply. Same for `tools` (Strata must emit OpenAI `tool_calls`) and `vision` (only
  if installed with `--vision`). Verify each on the pinned rev before declaring it.
- Attributes for `flash_next` (example; confirm values against the pinned rev and
  the chosen quant): `{"class": "llm", "family": "qwen", "params_b": 125,
  "arch": "moe", "quant": "<size>", "context_len": <configured>, "vision": false,
  "max_concurrent": 1}`. `params_b` must be the real total parameter count — the
  27B unit must never satisfy `params_b=[100,]`, and Flash-Next must never satisfy
  `params_b=[20,30)`.
- Residency: `UNPINNED`. Priority: numerically **higher** (less important) than
  `llm_general`. `min_residency_s`: at least the measured load time (anti-thrash);
  `reload_cost`: the measured load seconds. `spread_group: "llm"` (it IS an
  alternative to `llm_general`).
- `engine_source: {"repo": …, "rev": …, "model": "<size>", "model_sha256": …}`.
  harmony-llm verifies the installed tree's `git rev-parse HEAD` and the model file
  hash at node start; a mismatch marks the unit unavailable with that reason (it
  stays declared, so a request for it gets a 503 naming the mismatch rather than a
  silent fallback).

## 4b. Broad requests: characteristics decide, not names (user, 2026-10-02)

The user's words: *"the request [should be] as broad as possible — just because it
requests some characteristics will we decide to load Strata against, say, Qwen
27B."* So the acceptance requests are NOT `params_b=[100,]`-style clauses that
name Flash-Next in disguise. A caller states what the work needs or values; Harmony
picks between `llm_general` (27B, vLLM, 32 concurrent, 24K context, fast) and
`flash_next` (125B MoE, Strata, 1 concurrent, long context, slower) from that.

### What already exists and must be respected

`server.py` (~line 1727, the "REUSE ONE THAT IS ALREADY RESIDENT" comment): among
units satisfying a requirement, an already-resident one is used, and this is
deliberately **not** caller-settable, because a 27B swap costs ~50.7 s for
everyone. "Only a HARD requirement that the resident unit fails will pay for a
swap." Consequence the comment states: once an alternative is warm, indifferent
traffic follows it and does not swap back.

`prefer` exists (`livestack_node/preferences.py`) but only for ASR
(`ATTRIBUTES = {"asr.streaming", "asr.languages"}`, `METRICS = {"asr.quality", …}`)
and only orders survivors; the benchday doc forbids model/vendor labels as quality
evidence.

### What this change adds

1. **Discriminating characteristics, advertised or measured, never hand-typed
   when derivable:** `context_len` (launch line), `max_concurrent` (§4, launch
   line), `params_b` and `active_params_b` (model card; for MoE `active_params_b`
   ≈ the ~10-of-24,576-experts share), `arch` (`dense`/`moe`), and measured
   `llm.decode_tok_s` / `llm.prefill_tok_s` / `llm.first_token_ms` (harmony-llm
   already fits `prefill_tok_s`/`decode_tok_s` into `_FITTED_KEYS`; publish them as
   preference metrics with sample counts, like `asr.*`).
2. **HARD requirements that only one unit meets cause the load** — this is the
   existing rule, unchanged. Broad examples that must work as written:
   - `require: class=llm, context_len=[131072,]` → only Flash-Next qualifies →
     Harmony evicts the idle 27B and loads Strata.
   - `require: class=llm, max_concurrent=[8,]` → only the 27B qualifies → a warm
     Flash-Next is evicted when idle and the 27B returns.
   - `require: class=llm, params_b=[60,]` ("a big model", not a name) → Flash-Next.
3. **`prefer` for LLM characteristics** (`llm.params_b: max`,
   `llm.decode_tok_s: max`, `llm.context_len: max`, `llm.first_token_ms: min`), so
   a caller can say "the most capable model you have" or "the fastest" without a
   threshold. Default behaviour keeps the resident-reuse policy: **a preference
   alone never triggers a swap**, because the caller does not know the transition
   cost. A preference may trigger a load only when the request also carries a time
   budget (HARMONY.md "Speed intent — what an SLA deadline actually gates") that
   the planner's measured `reload_cost` plus the candidate's measured speed can
   meet; the ledger records the comparison. This keeps one indifferent request from
   costing everyone 50 s while letting "best model, I can wait 3 minutes" load
   Strata. **Q4** asks the user to confirm this rule.
4. **Concurrency protects the 27B's traffic.** Because indifferent traffic follows
   a warm unit, a warm Flash-Next would otherwise absorb broad `class=llm` traffic
   one request at a time. Rule: the resident-reuse shortcut applies only while the
   resident unit is below its `max_concurrent` and its queue (§4) is empty;
   otherwise the request goes to the planner like any other, which may restore the
   27B when Flash-Next goes idle. Callers that name `local`/`llm_general` (hub
   titles) are unaffected by this rule — named is named (server.py ~line 1658).

No new grammar is needed for 1–2 (intervals already exist). 3 extends
`preferences.py`'s vocabulary (`ATTRIBUTES`/`METRICS`) and wires `prefer` into
harmony-llm's selection (`_selection_rank`), which today ignores it.

## 5. Placement decision: card 1, not card 0

- **Card 1 (chosen).** Only tenant is `llm_general`, UNPINNED. The planner can
  evict it with no residency change. Cost: while Flash-Next is resident, any request
  for `llm_general` (hub titles, attention, `model: "local"`) waits for Flash-Next's
  `min_residency_s` to lapse, its idle eviction, and the 27B's ~50.7 s reload. The
  hub's title timeout is 35 s, so **titles fail during that window**. This is the
  honest cost of one card for two big LLMs, and the planner's `_contention_cost`
  will price it once demand is observed. Open question **Q2**: is that acceptable,
  or must Flash-Next only be admitted when `llm_general` has been idle for N minutes?
  If the latter, express it as a planner rule in the language (e.g. a unit-level
  `preempt_only_idle_for_s`), not as a hand-made schedule.
- **Card 0 (rejected for now).** `asr` is HARD_PIN there; an exclusive unit can
  never be admitted without changing that pin. Changing ASR's residency is a policy
  decision about media-corpus ingest and live dictation, out of scope.
- **zz-tower0 (rejected).** 29 GB RAM < Strata's 32 GB floor. Note for later: with
  ≥64 GB RAM it is the better host (Ryzen 5 7600 AVX-512 matches Strata's benchmark
  rig) and its card has no LLM tenant.

## 6. Ledger obligation

Every placement decision already leaves a ledger record (`ledger.py`,
`_plans/decision-ledger.md`). This change must make the record carry:
- the host-pool arithmetic (`ram_bytes` need, host free, reserve) for any unit with
  `ram_bytes`;
- `exclusive_device` and the tenants evicted to satisfy it;
- the refusal reason naming the blocking rule/tenant;
- the engine name and `engine_source.rev` of the unit loaded (from the node's
  report — the planner still does not interpret it).

## 7. Rejected alternatives

- **Run Strata standalone beside Harmony and let `--before-load` call Harmony to
  evict.** Placement decided outside the planner; Harmony would not know 45 GB of
  host RAM is pinned. Exactly what this change exists to end.
- **Declare Flash-Next's `vram_bytes` as 24 GB and ignore RAM.** Makes the card
  exclusive by accident and leaves host RAM unaccounted; the next RAM-heavy unit
  overcommits the host silently.
- **A per-device `ram_bytes` split.** Wrong on any multi-card host (§3).
- **A Strata-specific branch inside `livestack_node`.** The planner must stay
  engine-agnostic; engines are a node concern.
- **vLLM CPU offload (`--cpu-offload-gb`).** Moves weights over PCIe every forward
  pass; for a 60+ GB model on one card it is unusably slow. Strata exists because of
  this.

## 8. Rollout

1. Land engine adapters with vLLM only; deploy to xc-tower-ubuntu's harmony-llm;
   verify `llm_general` unchanged (same argv in the start log, same measured cost,
   titles still served).
2. Land planner host pool + `exclusive_device` + `max_concurrent`; deploy hostd on
   xc-tower-ubuntu. Fleet brokers elsewhere may run older code (zz-tower0's checkout
   was 67 commits behind on 2026-10-02): new report fields must be optional and
   ignored by old brokers, and an old node's report must still parse in the new
   broker.
3. After Q1 is answered: install Strata at the pinned rev, add `flash_next` to
   `/etc/harmony/llm-units.json` (a config change, allowed — it declares a unit; it
   does not force anything), restart harmony-llm once. Then run the acceptance
   scenarios **only through requests**.
4. Rollback: remove the unit from `llm-units.json` and restart harmony-llm; the
   planner then never sees it. Strata's files may stay on disk.

## 9. Verify-first items (do these before building on them)

- **fleet_rank requirement filter.** Reported: `GET /fleet/rank?kind=llm` ignores
  `require=` for LLM nodes, so a warm Strata node could become the hub's fallback
  for plain `llm` traffic. Reproduce with a test against `fleet_rank.rank()` before
  changing it; if it does not reproduce, record that here and drop the task.
- **Strata reasoning separation, tool calls, `/status` READY field** on the pinned rev.
- **Which `/proc` field carries Strata's pinned memory** (positive control, §2).

## 10. Open questions (for the user; do not guess)

- **Q1.** Which Strata fork and revision? (`architectds/Strata` at `36fa455` was
  surveyed; its README points at `Niko1221/Strata`. The user said a specific fork
  version is required.) Also which model size: `Q2_0` (fastest, 37.6 GB) vs
  `IQ2_XS` (recommended for 64 GB hosts) vs `IQ3_S` (best, slowest).
- **Q2.** Is it acceptable that a Flash-Next session evicts `llm_general` and hub
  titles fail for ~1–2 minutes around each switch (§5)?
- **Q3.** Context length for `flash_next`: Strata supports up to 262K; larger
  context costs RAM/VRAM. Default proposal: 131,072.
- **Q4.** May a `prefer` (e.g. "most capable") trigger a swap when the request also
  carries a time budget the reload fits in (§4b.3)? Or must a swap always come
  from a hard requirement the resident unit fails?
