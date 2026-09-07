# harmony-llm — the reference Harmony LLM node

A local LLM (vLLM) served as one or more Harmony **units**, so the card it sits
on is a decision the planner makes rather than a line in a service file.

This lived only as an unversioned `server.py` on one machine, hand-edited in
place, with `.bak-<timestamp>` copies beside it as the entire safety net. Two
concurrent agents editing it on 2026-09-06/07 is what made that untenable: the
file is fleet infrastructure and belongs where changes are reviewable.

## Shape

* vLLM runs as a **subprocess** per unit; the unit's loader/freer start and stop
  it. That is what makes eviction real — terminating the process is the only way
  to return VRAM to the driver.
* **One node process per card**, because a wrapper's CUDA meter reads the
  pressure the planner acts on, and a wrapper that saw both cards would report
  card 0's pressure for a model on card 1. The pin is a *metering* fact.
* **Every node declares every unit.** Which card a model is resident on is the
  planner's answer, derived from queued demand: two models in one
  `spread_group` that cannot co-reside cost each other in proportion to the
  demand waiting for them, so alternating traffic settles them one per card and
  stops paying for the separation when the alternation stops.
* **Admission before load.** A request calls `livestack_node.client.admit()`
  first: the broker plans, evicts victims on their own nodes, warms the grant,
  and answers with the device. Loading straight off the request is how a node
  starts vLLM into whatever memory happens to be free —

      ValueError: Free memory on device cuda:0 (6.9/23.56 GiB) on startup is
      less than desired GPU memory utilization (0.62, 14.61 GiB)

  with 10 GB of idle ASR and TTS on that card that nobody had asked to move.
  Enable with `HARMONY_LLM_ADMIT=1` on any node that shares its card.
* **Residency is PER UNIT** (`"residency"` in the spec), falling back to the node
  default. One node serves models with different claims on the card: the hub's
  title model must stay warm because a cold start costs a title, while an eval
  model used a few times a day must not. With one policy for the whole node, the
  eval model's SOFT_PIN restore kept re-claiming a card that cannot hold both and
  evicted titles each cycle.
* **`coload` is on when a node declares several units.** `coload=False` means
  acquiring one unit evicts the others IN THIS PROCESS, which is right for a
  single-model node and wrong the moment one node holds several: the broker's
  restore of unit A then fights its demand-warm of unit B, each load evicting the
  other and neither finishing. With several units, eviction belongs to the
  planner, which knows the footprints and the whole card.
* **Admission is for LOADING, not for every request.** A unit already resident
  here has been through admission and is serving; re-asking the planner for
  permission to use what is on the card turns a working model into a 503 on
  every call.
* A node asked for a unit the planner placed elsewhere **forwards** to the node
  that holds it (peer URLs come from the broker's `/peers`; `/status` carries no
  address). Otherwise it would load a second copy on its own card and be
  choosing placement again.

## Config

`HARMONY_LLM_UNITS_FILE` — a JSON array of unit specs (see
`llm-units.example.json`). Use a FILE, not an inline `Environment=`: systemd
processes quotes and strips the JSON's property-name quotes, which fails as
`Expecting property name enclosed in double quotes: line 1 column 3`.

Unset, the node serves exactly one unit named `llm` from the single-model
variables, byte-for-byte as before units existed.

| variable | meaning |
|---|---|
| `HARMONY_LLM_UNITS_FILE` | path to the unit specs |
| `HARMONY_LLM_SPREAD_GROUP` | contention class (default `llm`) |
| `HARMONY_LLM_ADMIT` | ask Harmony for room before loading |
| `HARMONY_LLM_RESIDENCY` | node default: `SOFT_PIN` / `UNPINNED` / `HARD_PIN` |
| `HARMONY_LLM_COLOAD` | let several units be resident (implied by >1 unit) |
| `HARMONY_LLM_CUDA_DEVICE` | the card this node speaks for |

`systemd/` holds the deployed units for a two-card host: card 1 `SOFT_PIN`
(a cold start there costs a hub title, measured ~50.7 s against a 35 s timeout),
card 0 `UNPINNED` with a 10-minute idle evict because it shares with polyasr and
polytts.

**This is a copy of a deployed file, not the deployment.** Changing it here does
not change the running service; sync deliberately.
