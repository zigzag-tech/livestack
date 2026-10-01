# Composition replay vs the engine — validation (2026-09-30)

**Status:** tool built, model gap measured, model fix not yet done.

`python -m livestack_node.replay_validate [--hours N]` replays the demand log of
each engine lifetime through `composition_replay` for the live composition. It
samples the model at the timestamps of vLLM's 10-second stats lines and reports
agreement on running sequences, waiting (yes/no) and KV usage. Waiting samples
are bucketed by the engine's own KV usage, so slot-bound and prefill-bound
queueing can be told apart. Lifetimes are delimited by harmony-llm's `vLLM ready`
/ `stopping vLLM` lines; a `systemctl restart` logs no stop, so a second ready
closes the previous lifetime.

## Baseline: 2026-09-30 15:51–20:26 EDT (chips+jemm, fp8 KV, 37,981 KV tokens)

1,455 samples, 6,254 records. The records predate the `n` field, so the hub's
`n=12` chip calls replay as single sequences.

| | engine | model |
|---|---|---|
| mean running sequences | 2.13 | 1.47 |
| max running | 8 | 8 |
| samples with requests waiting | 391 (27%) | 0 (recall 0) |
| mean KV usage | 0.26 | 0.05 |

Where the engine had requests waiting, by its KV usage: 279 of 391 at 70–90%, 46
at 90–100%, and 66 below 70%. Most queueing is the KV pool running out, which the
model never does.

## Why: a fixed per-sequence KV cost the model does not charge

From the engine's own samples with nothing waiting (KV tokens = usage × 37,981):

| running | KV tokens | per sequence |
|---|---|---|
| 1 | 5,522 | 5,522 |
| 2 | 10,179 | 5,089 |
| 4 | 18,706 | 4,676 |
| 8 | 36,842 | 4,605 |

The linear fit is about 1,050 + 4,510 × running. The median request is 650
tokens (mean 744, p90 1,398), so the cost is mostly independent of length. vLLM
logged at startup that it set the attention block size to **784 tokens** "to
ensure that attention page size is >= mamba page size". Every sequence therefore
holds whole 784-token blocks plus its linear-attention (GDN) state pages, about
5–6 blocks in all for a short request. It also sets the ceiling:
37,981 / ~4,600 ≈ 8 concurrent sequences, which is the observed maximum.

## Fix (next)

- **Charge each job** `ceil(prompt/block)·block + seqs · (ceil(completion_per_seq/block)·block + state)`,
  with `block` parsed from the startup line.
- **Fit `state`** per (base, KV dtype) from the engine's own samples, as above, and
  store it with the measured cost.
- **Then re-run this validation** with records that carry `n` (since 2026-09-30
  20:26 EDT) and un-skip `test_replay_matches_journal_end_to_end` with the measured
  tolerance.
- **Prefill-bound queueing:** the 66 samples below 70% KV may be prefill
  throughput (about 690 prompt tokens/s observed) or batch slots taken by `n=12`.
  Re-check after the fix.

## Also found: llm_general was down 2026-09-30 05:29–15:42 EDT

The host broker evicted `llm_general` three times ("preempted by
qwen_image_2512 (prio 30)", equal priority, UNPINNED and idle). harmony-llm's
fallback ("broker temporarily forgot {...}; using locally declared llm_general")
then restarted vLLM without admission about every 40 s, and the broker's reconcile
evicted it each time (05:30–05:57). The unit stayed down until 15:42. Not caused
by the measured-admission change (the preemption arithmetic is identical under
declared and measured footprints). The fallback that fights the broker is a defect
of its own.

**Fixed 2026-09-30:**
- The fallback now loads locally only when the broker's own defer reason is
  "no unit satisfies" (it did not know the unit). A refusal of a known unit
  ("no device can fit…", "residency floor…") returns 503 naming the reason, and
  a reply with no readable reason counts as a refusal.
- A failed vLLM start is not respawned for 30 s, doubling to 10 min; a load the
  broker explicitly grants skips that wait.
- `/admit` replies now carry `defer_reason` (host broker, `main`); harmony-llm
  also reads it from the plan summary that the deployed broker release already
  sends.
- Tests: `tests/test_harmony_llm_fallback.py`.
