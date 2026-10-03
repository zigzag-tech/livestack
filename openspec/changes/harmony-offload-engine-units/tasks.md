# Tasks — harmony-offload-engine-units

Read `proposal.md` and `design.md` (especially §0 ground rules) before starting.
Work in a worktree: `git -C ~/livestack worktree add -b agent/<task>
~/worktrees/livestack/<task> origin/main`. Tests run with
`~/livestack/node-py/.venv/bin/python -m pytest` under `nice`; heavy builds do not run
on xc-tower-ubuntu (benchday `AGENTS.md` rule 15) except where the design says so.
Land with `git -C ~/livestack merge --no-ff agent/<task>` from the main checkout and
push to origin main (livestack's convention; check `git log --first-parent` for the
merge-message style). Every task names its tests and its ledger obligation.

**Hard rule for every task (design §0):** never force-load. Nothing in a test,
deploy or verification step may call Strata's `/load`, Harmony's `/model/warm`,
start an engine by hand, or set `warm_on_start`/`default`/a pin on `flash_next`.
Residency is caused only by requests written in the request language, and those
requests are **broad**: characteristics, not unit names (design §4b).

## 1. Engine adapters (no behaviour change)

- [x] 1.1 Create `node-py/examples/harmony-llm/engines/` with the `Engine` protocol
  (design §2) and `vllm.py` holding the moved argv/ready/measure/stop code.
  `server.py` `_load`/`_free`/`_vllm_up`/`_attributes_for` delegate by
  `spec.get("engine", "vllm")`. `server.py` must shrink, not grow (2052 → 1990
  lines net; the moved code lives in `engines/`).
  Tests: all existing harmony-llm tests unchanged and green;
  `test_engines_vllm.py` pins `llm_general`'s real spec (from
  `/etc/harmony/llm-units.json`) against the pre-refactor argv as a fixture.
  Ledger: none.
- [x] 1.2 Unknown `engine` value → refused at STARTUP, naming the engine (the
  delta spec's "Unknown engine at startup" scenario — the softer "declared
  unavailable / 503" wording below it is superseded by the spec). Tests:
  `test_engines_unknown.py`. Ledger: none.

## 2. Planner: host pool, exclusive devices

- [x] 2.1 `planner.py`: `WorldState.hosts`, `HOST_DIMS = {"ram_bytes"}`, fit on
  device AND host pool; eviction returns `ram_bytes`. A unit with `ram_bytes` on a
  host with no measurement is refused `host memory unmeasured`.
  Tests: `tests/test_planner_host_pool.py` (11 cases: two RAM-heavy units on two
  cards of one host cannot both be placed; evicting one admits the other; units
  without `ram_bytes` place exactly as before — the existing planner tests
  unchanged and green).
  Ledger: placement record carries `host_pool: {need, free, reserve}` (design §6).
- [x] 2.2 `planner.py`: `Unit.exclusive_device`; charged the device's whole
  capacity; refusal names the blocking tenant and its residency.
  Tests: `tests/test_planner_exclusive.py` (evicts an idle UNPINNED tenant even
  though it is MORE important — the claim is on the space; refused with the
  tenant named when a HARD_PIN is there; Defer while the tenant is busy; nothing
  co-places; the resident exclusive never reads as over-budget pressure).
  Ledger: record lists evicted tenants and the exclusive flag.
- [x] 2.3 `hostbroker.py` / `hostd.py`: `hosts` assembled from the peers'
  `host_mem` (`_host_pools`: freshest per host, reserve off the top, loading
  claims subtracted — host-memory-ledger §3's arithmetic); `ram_bytes`,
  `exclusive_device`, `engine`, `engine_rev` carried from node reports; all new
  fields optional, an old node's report parses unchanged.
  Tests: `tests/test_host_pool_report.py`. Ledger: engine + rev on load records.
- [x] 2.4 `manager.py`: `ManagedUnit.footprint` accepts an `int` (VRAM bytes, as
  today) or a `Res` dict; the `/residence` report sends a vector.
  Tests: `tests/test_manager.py` + one dict-footprint case. Ledger: none.

## 3. Concurrency and LLM characteristics

- [x] 3.1 `Engine.launch_attributes` derives `max_concurrent` (vLLM:
  `--max-num-seqs` or vLLM's own default, named in `engines/vllm.py`; Strata: the
  serve server's one-sequence FIFO = 1 unless declared) and `context_len`
  (vLLM: `max_model_len`; Strata: declared — context is baked into its engine
  config). Published in unit attributes.
  Tests: `test_engines_attributes.py`. Ledger: none.
- [x] 3.2 harmony-llm per-unit admission queue (`unit_queue.py`): at most
  `max_concurrent` in flight, bounded FIFO of 64 waiting, 429 with the queue
  state beyond that; depth on `/residence`, `queue_ms` on the demand record.
  Tests: `test_unit_queue.py` with a stub engine that sleeps. Ledger: none
  (request-level, not placement).
- [x] 3.3 Resident-reuse shortcut applies only while the resident unit is below
  `max_concurrent` with an empty queue (design §4b.4). Named units unaffected.
  Tests: `test_unit_queue.py` (saturated unit routes instead of absorbing) +
  `test_llm_preferences.py` (the warm unit still answers). Ledger: the routing
  decision records why the shortcut was skipped (`shortcut` on the demand row).
- [x] 3.4 `preferences.py`: `llm.params_b` (max), `llm.context_len` (max),
  `llm.decode_tok_s` (max, measured), `llm.first_token_ms` (min, measured);
  `selection.py` publishes the measured ones from harmony-llm's fitted keys with
  sample counts. `prefer` wired into unit selection as ordering among survivors
  (`harmony_prefer` beside `harmony_requires`; ordering-only — the time-budget
  swap is Q4, not implemented).
  Tests: `test_llm_preferences.py`. Ledger: the selection record carries the
  `preference_key` receipt on the demand row.
- [x] 3.5 Context re-route (design §4c gap 1): `Engine.context_refusal`; on a
  context refusal the need (`context_len>=input+max_tokens`, the caller's own
  clauses ANDed in) re-routes ONCE through the normal admission path; a named
  request keeps its unit and the 413; nothing loops.
  Tests: `test_context_reroute.py` (two stub units, 24K/128K windows): the broad
  over-long request is answered by the 128K unit with no caller resubmission and
  exactly two upstream calls; a request too long for every unit gets the existing
  413 text with the need named; one re-route at most; streamed and non-streamed
  bytes pass through untouched; an unrelated 4xx keeps its model.
  Ledger: the re-route is recorded (one demand row for the request that was
  served).

## 4. Verify-first items (design §9)

- [x] 4.1 REPRODUCED: `fleet_rank.rank()` ignored `require=` for LLM nodes — a
  target whose units fail the clauses was still ranked. Fixed: candidates are
  filtered with a named `filtered` outcome + reason (and the unfiltered path is
  pinned by a positive control). Tests: `tests/test_fleet_rank.py` (+27),
  `tests/test_hostbroker.py` green. Ledger: none.

## 5. Strata engine (Q1 resolved: architectds/Strata tag v0.1.24-linux-cuda12.8, Q2_0)

- [x] 5.1 `engines/strata.py`: argv drives the rev's REAL entry point
  (`.venv/bin/python serve/server.py --engine strata --config strata-*.json
  --port N --host 127.0.0.1` — the `run-<model>.sh` line, since the pinned rev
  has no `llama-server` binary; design §1: "whatever the repo's actual script
  name, the adapter owns the exact argv"); `--idle-unload`, `--lazy`,
  `--before-load` are REFUSED (lifecycle is Harmony's). ready = `/health` AND
  `/status` state. measure = the engine's slot/GiB lines + NVML VRAM + the
  `/proc/<pid>/status` pin field (`VmLck`, chosen by a positive control),
  `source: "strata-startup"`, `unknown` with what never came named when nothing
  answers. `engine_source.rev`/`sha256` checked at node start against
  `STRATA_VERSION`/`MODEL_SHA256` (mismatch = startup error naming both).
  Tests: `test_engines_strata.py` against a fake Strata server (the serve
  server's API surface, incl. `/health`, `/status`, `/v1/chat/completions`).
  Ledger: none.
- [x] 5.2 VERIFIED on the pinned rev (`strata.sh verify`, exit 0 — receipts/):
  reasoning returned SEPARATELY (`reasoning_content`) -> `thinking: true`
  declared; OpenAI `tool_calls` work -> `tools: true` declared; `GET /status`
  reports `{busy, queued}` (the ready gate is `/health` AND `/status` state);
  pinned memory is `/proc/<pid>/status` `VmLck` (positive control:
  `tests/test_engines_strata.py`). Process name is `serve/server.py --engine
  strata` — the pinned rev has no `llama-server` binary; the adapter drives the
  real entry point (design §1: "whatever the repo's actual script name").

## 6. Deploy on xc-tower-ubuntu and acceptance (BLOCKED on Q1; Q2 informs it)

- [ ] 6.1 Disk: `diskreap scan`, then `diskreap clean --apply` (SAFE only) until
  ≥100 GB free. REPORT-ONLY items go to the user.
- [ ] 6.2 Install Strata at the pinned rev with
  `./setup.sh --yes --family qwen --model <size> --no-start` (model choice from Q1).
  Never let setup start the server. Record paths in harmony-llm `README.md`.
- [ ] 6.3 Deploy tasks 1–3 code to xc-tower-ubuntu's harmony-llm and hostd; verify
  `llm_general` unchanged (same argv in the start log, titles served, same measured
  cost). Add harmony-llm's systemd unit to the workload worker's `host_services`
  (host-memory-ledger) so batch placement sees Strata's RAM.
- [ ] 6.4 Add `flash_next` to `/etc/harmony/llm-units.json` on the card-1 node:
  `engine: "strata"`, `engine_source`, `exclusive_device: true`, `ram_bytes` prior,
  `residency: "UNPINNED"`, priority below `llm_general`, `min_residency_s` ≥ measured
  load time, honest attributes (design §4). Restart harmony-llm once. Confirm
  `/status` on :8799 lists it, **not resident**.
- [ ] 6.5 Acceptance — each scenario in the delta spec, driven only by requests,
  evidence = request, response, hostd `/status` before/after, ledger record:
  (a) a broad long-context request loads Flash-Next by evicting the idle 27B;
  (b) a broad request the 27B also satisfies, sent while the 27B is resident,
  does NOT swap; (c) a `max_concurrent=[8,]` request restores the 27B after
  Flash-Next idles; (d) `model: "local"` always reaches `llm_general`;
  (d2) a broad request whose prompt exceeds 24,576 tokens, sent with no
  `context_len` clause, is answered by Flash-Next after one internal re-route;
  (e) a request for something nothing satisfies is a 503 naming the rule;
  (f) host RAM stays inside the pool (no OOM; record swap before/after);
  (g) ASR on card 0 stays resident and serving throughout.
  Measure and record decode/prefill tok/s and load time on this host.
- [ ] 6.6 Docs: `HARMONY.md` "Engines other than vLLM" + host pool in
  "Context-awareness"; `_plans/resource-planner.md` §2 stale line;
  harmony-llm `README.md`; benchday `docs/livestack-harmony.md` (request examples
  for broad LLM requests and `prefer`); `~/.claude/docs/mesh.md` xc-tower-ubuntu
  host state. Then `openspec archive harmony-offload-engine-units`.
