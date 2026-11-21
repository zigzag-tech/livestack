# Add a model (or anything else) for Harmony to run

You are an agent on some fleet machine, and something new should be served by the
fleet: a model, an engine, or a job type. This runbook is served by the fleet
broker (MCP tool `harmony_runbook`, or `GET /runbooks/add-model`). Its source is
`livestack/node-py/livestack_node/runbooks/add-model.md`: edit it there, and
redeploy the broker to update every agent.

**The one rule: callers state a NEED and never name a host, card or unit.**
"Adding" therefore means declaring a unit whose attributes answer the needs
callers will write. Everything else (discovery, loading, eviction, leases,
routing) Harmony already does.

## 1. Pick the route

| What you are adding | Route | Where it is declared |
| --- | --- | --- |
| An LLM or embedder that vLLM or Strata can serve | **A. Units file** (config only, no code) | `/etc/harmony/llm-units.json` on the GPU host, served by `harmony-llm` (`node-py/examples/harmony-llm/`) |
| Any other model behind a Python server (ASR, TTS, OCR, a custom engine) | **B. `attach()` node** | the server's own `units=[...]` passed to `livestack_node.attach(...)` |
| An image model (generation, editing, upscaling) on Diffusers or similar | **C. Image worker** | a worker config for `livestack_node.imagegen` (`node-py/examples/harmony-image/README.md`) |
| A CPU job (build, test, render, batch work), not a resident model | **D. Workload handler** | a handler on the workload authority's workers (benchday `docs/harmony-worker-enrolment.md`) |

If more than one fits, prefer the higher row: it means less code and fewer moving parts.

## 2. Look before you write

Call these first, and paste what they return into your notes:

1. `harmony_vocabulary`: today's classes, attribute names and values, kinds per
   host, and every card's tenants and free memory.
2. `harmony_plan_unit` with your proposal:
   `{kind, host, attributes: {class, ...}, footprint: {vram_gb, ram_gb}, exclusive_device?, caller_need?}`.
   Fix every `error`. Read every `warning`, and either act on it or say why not.
   It reports:
   - fit per card: `fits_now`, `fits_after_eviction` (and what gets evicted), `never_fits`;
   - kind collisions and new or misspelt attribute names;
   - whether generic demand would start landing on your unit.

Choosing attributes:

- **Reuse names that exist** (`class`, `family`, `params_b`, `context_len`,
  `thinking`, `tools`, `vision`, `modality`, `task`, `quant`, `backend`,
  `model`). A new name is new vocabulary that callers have to learn. Add one only
  when no existing name says it.
- **`class` is required.** The localhost router forwards only `llm` (chat and
  completions) and `embed` (embeddings). A new class needs an ingress (route C
  has one), or a router path added in `examples/harmony-llm/router.py`.
- **Specialists must not catch generic demand.** If your unit answers general
  requests badly (OCR, depth guidance, inpainting), declare its specialist
  attribute. Only demand that names that attribute may reach it
  (`_SPECIALIST_ONLY_ATTRS` in `planner.py`; extend the list for a new
  specialist kind).
- **Thinking is two things.** `thinking: true` only if the engine separates
  reasoning (vLLM `--reasoning-parser`). Otherwise narration leaks into
  `content` (benchday `docs/livestack-harmony.md`).
- **The footprint is weights plus peak activation, measured.** It is not the
  checkpoint size. Load the model once and read the peak.

## 3. Pick the host and card

- Use the `fit` rows. `fits_now` is best. Accept `fits_after_eviction` only if
  you are willing to evict what it names.
- **`exclusive_device: true` holds the whole card while resident.** On
  2026-11-04 a FLUX job holding both GPUs starved speech for a day. Do not put an
  exclusive unit on a card that serves speech (`polyasr` or `polytts` tenants)
  unless the owner agreed.
- Host RAM matters for engine units (Strata pages experts from RAM): `ram_gb` is
  planned against the host pool.
- GPU hosts and their roles: `~/.claude/docs/mesh.md` and `docs/mesh.md` in xc-setup.

## 4. Write it as tracked config, never only on the host

The host copy is applied FROM the repo. An untracked edit under `/etc/harmony`
is flagged `drift: unexplained`, which wakes the overlord.

- **Route A**: edit `xc-setup/config/machines/<host>/harmony-brokers/llm-units.json`
  (copy a sibling unit's shape: `name`, `model`, `port`, `footprint_gb`,
  `residency`, `priority`, `attributes`, plus the engine fields). Install it with
  `sudo install -m 644 <file> /etc/harmony/llm-units.json`.
- **Route B**: the server lives in its own repo (polytts, polyasr, ...). Its
  systemd unit goes in `xc-setup/config/machines/<host>/systemd-user/` and is
  applied with `scripts/machine-config.py apply --host <host>`. That tool never
  restarts anything.
- **Route C**: the worker JSON and systemd unit go under
  `xc-setup/config/machines/<host>/` like the existing `harmony-image*.json`.
- **Route D**: follow benchday `docs/harmony-worker-enrolment.md`. Its handler
  release and worker config are immutable artifacts, not checkouts.
- Credentials never go in the repo (`<redacted>`; `machine-config.py` restores
  the host's own value).

## 5. Deploy at idle

- Check that the card is not mid-request: `GET http://<host>:8188/health`, or
  the node's `/livestack/capability`, or the fleet view's `busy`.
- Restart only the service you changed. A node announces itself to its broker
  on start, and every 30 s after, so membership needs no other step.
- Never restart the shared brokers (`livestack-hostd`, the fleet brokers) to
  pick up a unit. A restart is needed only for a broker code release.

## 6. Prove it

1. `harmony_match_need` with the need callers will send. Your unit must appear
   in `matches`, and `inexpressible` must be absent.
2. From a machine with a router, ask where the request would go, then send one
   real request:
   - `POST http://127.0.0.1:8188/harmony/route` with the request body returns
     the chosen node, without sending anything;
   - then send one real request and check the answer, not just the status code.
3. The first real request may cold-load the model. Expect the measured
   `load_s` to appear in the fleet view's `inventory` afterwards.

## 7. Record it

- Commit the config with a message that says what the unit is, its measured
  footprint, and the proof from step 6.
- If callers need to know (a new class, a new attribute), add one line to
  livestack `HARMONY.md` beside the units it joins.
- If this runbook was wrong or missing a step, fix it in the same change. That
  is how the next agent gets it right.
