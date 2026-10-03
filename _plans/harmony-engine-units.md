# Harmony engine units (design record)

Realised by `openspec/changes/harmony-offload-engine-units` (archived
2026-10-02). This is the record of WHAT SHIPPED and what is now stale
elsewhere; the operator runbook lives in `HARMONY.md` ("Engine units").

## What shipped

1. **The engine seam** (`node-py/examples/harmony-llm/engines/`). What it takes
   to drive one unit's engine — argv, env, ready, measure, stop, and the
   attributes its launch line proves — lives behind `engine_for(spec)`; routing,
   admission, the queue and forwarding are engine-blind. Two engines: `vllm`
   (moved out of `server.py` byte-for-byte — `tests/test_engines_vllm.py` pins
   the launch line for xc-tower-ubuntu's real `llm_general` spec against the
   pre-refactor argv) and `strata`. An unknown engine is a STARTUP ERROR naming
   itself. `engine` is never a requestable attribute.
2. **Host RAM is a host-scoped planned resource** (planner `HOST_DIMS`,
   `WorldState.hosts`/`host_reserve`). Two 45 GB units fit two 24 GB cards
   separately and together swap the machine; `ram_bytes` is fitted against the
   host pool shared by every device on the host, eviction returns it there, and
   a host nobody measures is UNMEASURED — a `ram_bytes` unit is refused
   `host memory unmeasured`, units without one place as before. The placement
   record carries `{need, free, reserve}`.
3. **A unit may claim a whole device** (`Unit.exclusive_device`): charged the
   device's entire capacity, admitted only when every other tenant can leave —
   idle evictable tenants are evicted (priority does not shield them: the claim
   is on the space, not on the work), a busy one defers, a HARD_PIN refuses it
   by name. Its grant is "all of it"; nothing co-places with it afterwards.
4. **Per-unit admission** (`unit_queue.py`): `max_concurrent` from the launch
   line (`--max-num-seqs` for vLLM; the serve server's one-sequence FIFO for
   Strata unless declared), bounded FIFO of 64 waiting, 429 naming the queue
   state beyond it, depth and `queue_ms` on `/residence` and the demand record.
   A saturated resident unit is NOT a shortcut: the resident-reuse optimisation
   applies only below the limit with an empty queue, and the routing decision
   records why the shortcut was skipped.
5. **`prefer` orders the survivors** (`preferences.py` vocabulary
   `llm.params_b`/`llm.context_len`/`llm.decode_tok_s`/`llm.first_token_ms`;
   `selection.py` publishes what the launch line says plus what the engine
   measured, with sample counts). Ordering only — a preference alone never
   swaps a resident unit — and the selection record carries the
   `preference_key` receipt.
6. **A context refusal is a routing fact**: re-routed ONCE by requirement
   (`require:class=llm,context_len>=N` with the caller's own clauses ANDed in),
   only when another unit can hold the need; otherwise the established 413 with
   the need named. Response bytes, stream or not, pass through untouched.
7. **`fleet_rank` filters on `require`** (it did not: a warm LLM node became a
   fallback for traffic it cannot serve — reproduced, fixed, and regression-
   pinned in `tests/test_fleet_rank.py`).

## Stale elsewhere

* `resource-planner.md` / `planner.py` fit rules were device-only; a unit's
  host-RAM pin is now a second pool. Its `MEASURED_SOURCES` gained
  `strata-startup` (an engine's own report is its own report whatever the
  engine is called).
* `harmony-gaps-2026-09.md`'s engine gaps (non-vLLM engines, host-RAM units)
  are closed by items 1-3 above.

## What the queue and the re-route do NOT fix

This is the `streaming-restart-race` boundary, stated once so nobody reads the
queue as more than it is:

* **A streaming response still owns its slot until the last byte.** That is
  deliberate (a generation that is still producing tokens is still occupying
  the engine), and it means a client that stalls mid-stream holds a queue place
  for as long as it stalls. The bound (64 waiting) is the protection; there is
  no per-request timeout here.
* **The queue is per node, not per engine.** Two nodes sharing one engine port
  (the `HARMONY_LLM_PORT_OFFSET` trap) have two queues in front of one engine.
* **A restart during a stream is still a broken stream.** The queue and the
  re-route protect ADMISSION and ROUTING; neither keeps a model alive under a
  stream that outlives it. Eviction of a busy unit is idle-only preemption for
  exactly this reason.
* **The re-route does not re-measure.** The need comes from the refusal text
  (`at least N input tokens`) plus `max_tokens`; an engine whose refusal names
  no number gets the 413, not a guess.
* **The anti-thrash floor cannot see a peer-reported load.** `min_residency_s`
  compares against `Placement.loaded_at`, and a placement learned from a
  `/residence` report carries `loaded_at: 0` (`hostbroker.RestPeer.placements`)
  — so a freshly-loaded unit is protected only inside the plan that loaded it,
  and the next cycle's plan reads it as old. Found during the acceptance: a
  concurrent request path stopped a just-loaded Flash-Next the second it
  became ready (the same-second eviction also needed the `coload=0` trap below;
  both had to be true). The fix is for the broker to remember when a placement
  first appeared per (peer, kind, device) — it already does exactly this shape
  of bookkeeping in its sticky-residency state — and that is NOT in this
  change.
* **A loading node looks dead to its broker.** The facade blocks while it
  serves, so a 40 s engine load makes the broker's snapshot time out (`fresh
  -> suspect ... timed out`), its units vanish from the world, and concurrent
  requests fall back to the local catalogue (`broker temporarily forgot ...
  using locally declared`). The fallback is deliberate and named; the
  fleet-visible gap during a load is not fixed here.

## Open questions the change carried (Q1-Q4)

* **Q1 (fork, rev, model size) — resolved for this deployment**:
  `architectds/Strata` at tag **`v0.1.24-linux-cuda12.8`**
  (`e416ba57f589f7c2561a1ed14ef72d4358f14027`, in `STRATA_VERSION`) with the
  fork's own prebuilt engine (`strata-linux-x64.zip` from that release: 0.1.24
  for sm_80/sm_86/sm_89, CUDA 12.8), and model size **Q2_0** (37.6 GB of the
  host's RAM, 66 GB download — the fastest quant that fits). The rev is a
  FORK RELEASE, not a random commit: design §1 surveyed `36fa455` (v0.1.36
  source), but its engine can only be built locally and the build dies against
  this host's glibc 2.4x + CUDA 12.9 (`cospi` exception-specification conflict
  in `mathcalls.h` — CUDA fixed it in 13.0; the compile is recorded in
  receipts/). The tag pairs source, MIN_ENGINE and the published Linux
  artefact, so `setup.sh` installs the prebuilt instead of compiling. Both the
  rev and its artefact are recorded in `STRATA_VERSION` and `HARMONY.md`;
  re-pinning is `strata.sh setup --model <size>`. The rev's actual launch shape
  differs from design §1's sketch (`serve/server.py` + the engine config, not
  `./llama-server`) — the adapter owns the real argv, as §1 anticipated
  ("whatever the repo's actual script name").
* **Q2 (titles during a switch) — accepted, not solved**: during a Flash-Next
  residency the 27B's title calls wait for the floor, the idle eviction and the
  reload. The queue makes the wait VISIBLE (`queue_ms`); it does not make it
  short. The planner's floors (`min_residency_s`) bound it.
* **Q3 (context_len) — the default proposal held**: 131,072 for flash_next
  (the setup's `--context 131072`), declared in the units file (Strata bakes
  context into its engine config; the server's CLI has no flag to read).
* **Q4 (time-budget re-route) — NOT implemented**: a preference plus a stated
  time budget that would pay for a swap is the one swap case (§4b.3); until a
  budget exists in the request language, `prefer` is ordering-only and a
  resident unit keeps answering. The ordering half is what shipped.
