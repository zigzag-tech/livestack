# harmony-llm — the reference Harmony LLM node

A local LLM (vLLM) served as one or more Harmony **units**, so the card it sits
on is a decision the planner makes rather than a line in a service file.

This lived only as an unversioned `server.py` on one machine, hand-edited in
place, with `.bak-<timestamp>` copies beside it as the entire safety net. Two
concurrent agents editing it on 2026-09-06/07 is what made that untenable: the
file is fleet infrastructure and belongs where changes are reviewable.

## Deployment — this checkout IS the deployed source

`xc-tower-ubuntu` runs it: the systemd units execute
`/home/ubuntu/harmony-llm/venv/bin/python /home/ubuntu/harmony-llm/server.py`,
where that `server.py` is a **symlink to this file**. The deployment directory
holds the venv and nothing else. An edit here is an edit in production after a
restart; there is no copy step and there must not be one.

**Do not `git init` in the deployment directory.** That happened on 2026-09-07 —
"chore: put harmony-llm under version control", of a file this repository had
already been versioning — and it forked the deployed copy away for a month.
Five commits of real work landed in a repo with no remote, on one machine's
disk, absent from zz-tower0 and zz-tower2: the requirement grammar, the
resident-unit fix, `context_len` describing what a unit serves, two-sided
tool-calling derivation, and the 413 context refusal. `1d434b6` among them is
the hash `benchday/docs/livestack-harmony.md` cites as the fix that made the
request language usable — a citation that resolved nowhere shared.

The work is reclaimed and that history is kept as an archive ref:

    git log refs/archive/harmony-llm-fork

The paragraph above about `.bak-<timestamp>` copies being the entire safety net
is why this file was moved here in the first place. The fork undid that, so the
symlink is the structural fix: the drift cannot reopen while the deployed path
cannot hold its own bytes.

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

### One copy per host

Several nodes on one box (one per card) read the SAME units file, so every unit
is declared on every node. That must not mean every node loads it.

Before loading a unit, a node asks whether a peer of its own kind already
**holds** it — resident, or still loading — and forwards there instead. Both the
request path and warm-on-start do this. Without it, a two-card host ran two
21.7 GB copies of one 27B, filled both cards, and had nowhere left to put a 3 GB
embedding unit.

The planner enforces the same thing one layer up, and that is where the real
fix lives: the host broker records every load it dispatches, and a unit whose
load is in flight is handed to the planner as a `Placement(loading=True)`. It
holds its card and cannot serve yet, so the SOFT_PIN restore and the pin floor
both count it as present and place nothing beside it, while a request waits for
it instead of loading a second copy elsewhere.

Two things that do NOT solve this on their own, and why:

- **`/admit`.** It answers "where may I put this?", and for a node with a free
  card the honest answer is "your own card", every time. Correct, and the wrong
  question — which is why warm-on-start does not route through it.
- **Checking residency.** A 27B is `resident: false` for the minutes it takes to
  load — long enough for a peer to look, see nothing, and load its own copy.
  `/health` therefore reports `loading` beside `resident`: a vLLM that is
  starting is a *claim* on that unit, and a claim nobody can see is no claim.

Which node warms is an operator decision, per node, via
`HARMONY_LLM_WARM_ON_START`. Nothing elects it — a node cannot infer another's
willingness to warm, and a node that warms nothing still serves, by forwarding.

### Embedding units

A node can serve embeddings alongside generation. Declare a unit whose launch
line starts vLLM for pooling and the rest follows on its own:

```jsonc
{ "name": "embed_multi", "model": "Qwen/Qwen3-Embedding-0.6B", "port": 8205,
  "footprint_gb": 3, "gpu_fraction": "0.12", "max_model_len": "8192",
  "extra_args": "--task embed --max-num-seqs 32", "residency": "UNPINNED",
  "attributes": { "params_b": 0.6, "family": "qwen", "dim": 1024 } }
```

Note what is NOT declared: `class`. It is DERIVED from `--task embed` (or
`--runner pooling`, the v0.10+ spelling), because what kind of work a unit
serves is a launch-line fact like `thinking` and `tools` — a vLLM started for
pooling answers `/v1/chat/completions` with a 400 and vice versa, so a
hand-declared `"class": "llm"` beside `--task embed` is an attribute that lies:
it matches a chat request and the unit then refuses it.

Callers say `require:class=embed`, or nothing at all — `class=embed` is derived
from the `/v1/embeddings` path exactly as `class=llm` is derived from
`/chat/completions`, so an ordinary OpenAI embeddings call routes correctly with
no requirement string. Health probes follow the unit: a pooling unit is probed
with `/v1/embeddings`, never a chat completion it would refuse.

`systemd/` holds the deployed units for a two-card host: card 1 `SOFT_PIN`
(a cold start there costs a hub title, measured ~50.7 s against a 35 s timeout),
card 0 `UNPINNED` with a 10-minute idle evict because it shares with polyasr and
polytts.

**This is a copy of a deployed file, not the deployment.** Changing it here does
not change the running service; sync deliberately.
