# H09 — real-hardware Laya qualification (packets C, D, I)

Every number here comes from a JSON artifact in this directory, produced by a
script in `node-py/scripts/`. Nothing in this file is asserted from memory. To
reproduce:

```bash
# CUDA, on a host with an NVIDIA GPU and the pinned weights
LAYA_MODEL_DIR=~/models/laya-multilingual \
  ~/venvs/laya-cuda/bin/python scripts/h09_cuda_laya.py docs/decisions/h09/cuda-<host>.json

# MLX, on an Apple Silicon Mac with the converted weights
LAYA_MODEL_DIR=~/models/laya-multilingual-mlx \
  ~/venvs/laya-mlx/bin/python scripts/h09_mlx_laya.py docs/decisions/h09/mlx-<host>.json

python3 scripts/h09_parity.py docs/decisions/h09/cuda-<host>.json \
                              docs/decisions/h09/mlx-<host>.json \
                              docs/decisions/h09/parity.json

# capability probe, either backend
python3 scripts/laya_attention_probe.py {cuda|mlx} docs/decisions/h09/capability-probe-<backend>.json
```

Measured 2026-09-20. CUDA: RTX 3090 on `xc-tower-ubuntu`, upstream
`convaiinnovations/laya-multilingual`, laya 0.3.4. MLX: `xc-mac-studio`
arm64/Metal, `aac6fef/laya-multilingual-mlx`, laya-mlx 0.1.0 with `torch`
confirmed absent from the interpreter (the script refuses to run otherwise —
packet D's "never report a mocked MLX test as real Mac qualification").

## Verdict: both gates FAIL. The profile stays `*:unqualified`.

### 1. Cross-architecture parity fails (task 4.4) — `parity.json`

Tolerance is 0.01. Numerically and semantically:

| case | CUDA | MLX | Δ |
|---|---|---|---|
| short-status argmax | `working` (0.560) | `self_waiting` (0.624) | **disagree** |
| P(working) | 0.560 | 0.145 | 0.415 |
| P(self_waiting) | 0.154 | 0.624 | 0.471 |
| P(question) | 0.062 | 0.087 | 0.025 |
| max-context argmax | `finished_turn` | `finished_turn` | agree |
| noul c00 / c01 / c02 | 0.862 / 0.859 / 0.871 | 0.564 / 0.511 / 0.544 | 0.298 / 0.348 / 0.328 |

The noul rows are the load-bearing statement: at the C01 policy threshold of
0.65, **CUDA admits every sampled candidate and MLX drops every one**. The same
pane, the same policy and the same contract produce opposite slates depending
on which architecture the broker happened to place the work on. That is what a
shared logical profile is supposed to make impossible, so these two physical
kinds cannot share one today.

This is not a tolerance to be widened. A 0.3–0.47 probability gap is a
different model, not a port with rounding error.

### 2. The attention task itself fails, on both backends — `capability-probe-*.json`

Twelve hand-written unambiguous cases (EN + ZH), one per the six labels' clear
instances. This is a cheap capability probe, **not** Q01: Q01 scores sealed
production episodes. It was run first because if the clear cases fail, no
threshold fitted to production data can rescue them, and Q01 would spend real
sealed evidence to learn the same thing.

| backend | accuracy (chance 0.167) | **question recall** |
|---|---|---|
| CUDA | 0.500 | **0.0** (0 of 5) |
| MLX | 0.417 | **0.0** (0 of 5) |

Both backends score 0 on every blocking question, in both languages. On
`"I'm about to delete the migrations directory. Should I go ahead?"` the
`question` label ranks **6th of 6** (CUDA P=0.015, MLX P=0.036). The models are
not broken in general — `working`, `finished_turn` and `self_waiting` are
mostly correct and confident (CUDA 7/7 on the non-question cases) — they are
specifically blind to the one class this change exists to detect.

`question` is the entire product purpose of pane attention: it is what raises
needs-you. A classifier with zero recall on it cannot be the semantic leg of
the attention authority at any threshold.

## What this means for the linked change

- Do not enable `BENCHDAY_ATTENTION_DECISION_POLICY=laya`. The named lexical
  fallback (`asks_for_input()`) is not a degraded path here; it is the only
  thing detecting questions.
- The **chip** (noul) task is a separate gate and is not condemned by the
  question-recall finding, but it fails parity on its own numbers above.
- The 27B question-extraction leg (stage 2) is a different model and a
  different profile. Nothing here is evidence against it.
- Q01–Q04 remain unscored. They are now also lower value than a decision about
  whether the Laya attention leg should exist at all.
