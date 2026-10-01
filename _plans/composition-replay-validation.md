# Composition replay vs the engine — validation (2026-09-30)

**Status:** model fixed and validated on one engine lifetime (2026-09-30); provisional fit, refit on records with `n` scheduled.

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

## Fix (done 2026-09-30) and its validation

The model, `composition_replay`, now has four parts:

1. **Paged KV.** Every sequence holds `ceil(tokens/block) + state` pages. `block` is
   read from vLLM's startup line: 784 with bf16 KV and 1,568 with fp8 on this base.
   `state` is fitted from the engine's own stats lines (`replay_validate
   --fit-state`); here it is 1.79 pages.
2. **Samples are sequences.** A request with `n` samples is `n` single-sequence
   jobs (vLLM schedules them separately). The first sample carries the prompt; the
   others reuse it.
3. **Two-rate service.** Prompt tokens go through **one shared prefill server**
   (the engine's prompt throughput is a total), and then each sequence decodes on
   its own. The rates are fitted by least squares on records from moments when
   nothing was waiting: 711 prefill tok/s and 23.9 decode tok/s per sequence, which
   match vLLM's own throughput lines.
4. **Siblings wait for their shared prompt.** A sample that reuses a sibling's
   prompt cannot decode before that prompt has been processed.

Same lifetime as the baseline, with chip `n=12` inferred from
`completion_tokens > 64` (those records predate the `n` field):

| | engine | old model | new model |
|---|---|---|---|
| mean running sequences | 2.13 | 2.95 | 2.04 |
| mean KV usage | 0.265 | 0.049 | 0.263 |
| waiting recall / precision | — | 0 / — | 0.34 / 0.54 |

The averages match. Second-by-second waiting agreement is moderate, as expected
from a deterministic replay of jittery arrivals. Pinned by
`test_replay_matches_the_engine_on_a_real_lifetime` (fixture
`tests/fixtures/composition/replay-llm_general-2026-09-30.json.gz`); the old model
is the negative control.

**Provisional fit.** The values stored on the fp8 measured row
(`state_pages_per_seq` 1.791, `prefill_tok_s` 711.3, `decode_tok_s` 23.93) came
from the inferred-`n` window and say so in `state_fit.provisional`. Fitting the
same window without inferring `n` gives a biased 37.8 decode tok/s, because a
12-sample call reads as one sequence with 12x the output. A one-off refit on a day
of records that carry `n` is scheduled for 2026-10-01 23:30 EDT:
`livestack-replay-refit.timer`, report at
`~/.cache/livestack/replay-refit-2026-10-01.json`.

**The composer must not mix accounting models.** In the first dry run, the bf16
candidate had no measured block size, so it was priced with token accounting
(queue-free) against the paged fp8 live composition, and the composer recommended
it. Now a paged base with an unmeasured block size for a KV dtype is
`unknown:...:kv_block:<dtype>`. The bf16 row's block (784) was backfilled from
that start's own journal line. Every cost now records `kv_accounting`
(`pages:<block>x<state>,service:<P>/<D>`, or `tokens,service:blended`).

**fp8 vs bf16, measured (2026-10-01).** The paragraph that stood here assumed bf16
holds the same ~1.8 state pages per sequence as fp8, and concluded bf16 would queue
much less. That was wrong. Identical controlled bursts (`scripts/measure_kv_pages.py`:
K = 1..8 requests, 662-token prompt + 300 forced tokens, exact-K readings of
`vllm:kv_cache_usage_perc` only), first on a temporary bf16 chips-only engine
(00:22–01:07 EDT, two ~3-minute outages) and then on fp8:

| KV dtype | block | pages per sequence | attention pages | state pages | sequences that fit |
|---|---|---|---|---|---|
| fp8 | 1,568 | 2.94 (linear in K, intercept 0) | 1 | **1.94** | ~8.2 |
| bf16 | 784 | 4.22 (linear in K) | 2 | **2.22** | ~9.0 |

The fp8 burst agrees with the traffic fit (1.79) within 8%. bf16 holds about 15% more
state pages, so it fits only ~10% more short sequences, not the 38-vs-24 pages the
raw page counts suggest. State pages are now stored and used **per KV dtype**
(`kv_paging` prefers the candidate's own dtype). Both rows carry the burst values
(`state_burst`) so the dtypes are compared by the same method. Replayed on the same
traffic with these values, bf16 chips-only queues *more* than fp8 chips-only (2,690 vs
2,171 request-seconds per 6 h window).

**What the composer says now:** drop `jemm` (fp8, chips only), gain 999 vs change cost
971: a thin margin. `jemm` is unused by production traffic, and its adapter slot takes
~1 GiB of KV pool. Proposal only; applying it is the owner's call.

**Fitted values survive restarts.** A harmony-llm restart re-measured the engine and
replaced the whole measured row, wiping the fitted state pages and service rates (the
composer silently fell back to `service:blended`). `_record_measurement` now carries
the fitted fields (`_FITTED_KEYS`) forward.

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
