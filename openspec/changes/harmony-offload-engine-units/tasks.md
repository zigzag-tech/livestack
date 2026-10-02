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

- [ ] 1.1 Create `node-py/examples/harmony-llm/engines/` with the `Engine` protocol
  (design §2) and `vllm.py` holding the moved argv/ready/measure/stop code.
  `server.py` `_load`/`_free`/`_vllm_up`/`_attributes_for` delegate by
  `spec.get("engine", "vllm")`. `server.py` must shrink, not grow.
  Tests: all existing harmony-llm tests unchanged and green; new
  `test_engines_vllm.py` asserting the argv for `llm_general`'s real spec
  (copy it from xc-tower-ubuntu's `/etc/harmony/llm-units.json`) is identical to
  the pre-refactor argv (capture it from the old code in the test as a fixture).
  Ledger: none.
- [ ] 1.2 Unknown `engine` value → the unit is declared unavailable with reason
  `unknown engine <x>`, reported on `/residence`; a request for it gets 503 naming
  that. Tests: `test_engines_unknown.py`. Ledger: none.

## 2. Planner: host pool, exclusive devices

- [ ] 2.1 `planner.py`: `WorldState.hosts`, `HOST_DIMS = {"ram_bytes"}`, fit on
  device AND host pool; eviction returns `ram_bytes`. A unit with `ram_bytes` on a
  host with no measurement is refused `host memory unmeasured`.
  Tests: `tests/test_planner_host_pool.py`: two RAM-heavy units on two cards of one
  host cannot both be placed; evicting one admits the other; units without
  `ram_bytes` place exactly as before (run the existing planner tests unchanged).
  Positive control: the two-units test FAILS on the old planner.
  Ledger: placement record carries `host_pool: {need, free, reserve}` (design §6).
- [ ] 2.2 `planner.py`: `Unit.exclusive_device`; charged the device's whole
  capacity; refusal names the blocking tenant and its residency.
  Tests: `tests/test_planner_exclusive.py` — exclusive unit evicts an idle UNPINNED
  tenant; is refused (with the tenant named) when a HARD_PIN is there; waits (Defer)
  while the tenant is busy (idle-only preemption).
  Ledger: record lists evicted tenants and the exclusive flag.
- [ ] 2.3 `hostbroker.py` / `hostd.py`: assemble `hosts` from the `hostview.py`
  measurement the broker already reads (`host_mem`); carry `ram_bytes`,
  `exclusive_device`, `engine`, `engine_rev` from node reports. All new fields are
  optional; an old node's report parses unchanged (old brokers elsewhere in the
  fleet will ignore the fields — design §8).
  Tests: extend the hostbroker report-parsing tests with old- and new-shape reports.
  Ledger: engine + rev on load records.
- [ ] 2.4 `manager.py`: `ManagedUnit.footprint` accepts an `int` (VRAM bytes, as
  today) or a `Res` dict; the `/residence` report sends a vector. Tests: existing
  manager tests + one dict-footprint case. Ledger: none.

## 3. Concurrency and LLM characteristics

- [ ] 3.1 `Engine.launch_attributes` derives `max_concurrent` (vLLM: `--max-num-seqs`
  or the installed vLLM's default, read from the engine not hard-coded; Strata: 1)
  and `context_len`. Published in unit attributes.
  Tests: `test_engines_attributes.py`. Ledger: none.
- [ ] 3.2 harmony-llm per-unit admission queue: at most `max_concurrent` in flight,
  bounded FIFO of 64 waiting, 429 with a reason beyond that; depth on `/residence`.
  Tests: `test_unit_queue.py` with a stub engine that sleeps. Ledger: none
  (request-level, not placement).
- [ ] 3.3 Resident-reuse shortcut (`server.py` ~line 1727) applies only while the
  resident unit is below `max_concurrent` with an empty queue (design §4b.4).
  Named units (`local`, `llm_general`) are unaffected.
  Tests: a warm single-stream unit does not absorb a second broad `class=llm`
  request; a named request still goes to its named unit. Ledger: the routing
  decision records why the shortcut was skipped.
- [ ] 3.4 `preferences.py`: add `llm.params_b` (max), `llm.context_len` (max),
  `llm.decode_tok_s` (max, measured), `llm.first_token_ms` (min, measured); publish
  the measured ones from harmony-llm's fitted keys with sample counts. Wire
  `prefer` into harmony-llm's unit selection as ordering among survivors.
  A preference alone never causes a swap; with a time budget, only when the
  measured reload + speed fits it (design §4b.3, **blocked on Q4** — implement the
  ordering-only part first).
  Tests: `test_llm_preferences.py`. Ledger: selection records the preference
  receipt (`preference_key` already returns one).

- [ ] 3.5 Context re-route (design §4c gap 1): `Engine.context_refusal`; on a
  context refusal of an un-named request, derive `context_len>=input+max_tokens`
  and route once more through the normal path; named requests keep the 413.
  Tests: `test_context_reroute.py` with two stub engines (24K and 128K windows): an
  over-long broad request is answered by the 128K unit with no caller resubmission;
  a named one gets 413; a request too long for every unit gets the existing 413
  text; at most one re-route. Positive control: the broad case returns 413 on the
  old code. Ledger: the re-route record (original unit, need, chosen unit, load).

## 4. Verify-first items (design §9)

- [ ] 4.1 Reproduce the reported `fleet_rank` defect (`GET /fleet/rank?kind=llm`
  ignoring `require=`) in a test against `fleet_rank.rank()`. If it reproduces, fix
  it; if not, record "not reproduced" with the test in design §9 and close.
  Ledger: none.

## 5. Strata engine (BLOCKED on Q1: fork + rev + model size)

- [ ] 5.1 `engines/strata.py`: argv (no `--idle-unload`, `--lazy`, `--before-load`),
  ready (`/health` + `/status` loaded), measure (NVML VRAM, `/proc/<pid>/status`
  pinned field chosen by a positive control, Strata's slot/GiB log line,
  `source: "strata-startup"`), `engine_source` rev/model-hash check at node start.
  Tests: `test_engines_strata.py` against a fake Strata server process (a tiny
  HTTP stub implementing `/health`, `/status`, `/v1/chat/completions` with a
  one-request lock) — this is a unit test of the adapter; the real engine is
  covered by 6.x. Ledger: none.
- [ ] 5.2 On the pinned rev, verify and record in design §1: reasoning returned
  separately or inline (decides `thinking`), OpenAI `tool_calls` (decides `tools`),
  the `/status` READY field, which `/proc` field shows pinned memory.

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
