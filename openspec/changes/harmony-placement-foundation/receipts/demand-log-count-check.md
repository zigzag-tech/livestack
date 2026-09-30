# Demand-log count check — xc-tower-ubuntu, 2026-09-30 (task 3.3)

**Window.** vLLM counters reset at every engine start, and harmony-llm had been
restarted twice that afternoon (15:46 and 15:49 EDT, by someone other than this
change). So the window is the current engine's life: 15:51:30–18:22:56 EDT, 2.52 h.
The demand log itself has run continuously since 2026-09-28 04:58 UTC.

| source | count |
|---|---|
| `vllm:request_success_total` (stop 11,111 + length 1,788) | 12,899 |
| demand-log records (all `ok`, HTTP 200) | 3,668 |
| `/residence` `demand_log` | written 3,732 (since process start), dropped 0, write_failed 0 |

**The gap is not loss.** In a separate 15-minute window, the three request streams
agree:
- vLLM's own access log: 340 `POST /v1/chat/completions`;
- harmony's proxy access log: 340;
- the demand log: 336 (requests in flight at the window edges).

Every connection to vLLM's port in a 90 s sample came from harmony-llm itself (one
pid). The difference is **samples per request**. The hub's chip generation
(benchday `hub/src/adaptive-chips/lora-generate.ts`) sends two calls per chip, a
greedy one with `n=1` and a sampled one with `n=12`. vLLM counts each sample as a
finished request. The window had 1,679 chip records, about 840 pairs, so the
expected count is 3,668 + 11 × 839.5 ≈ 12,903, against 12,899 counted.

**What changed.**
- Demand records now carry `n` (absent in the body = 1, the OpenAI default).
- The replay model runs a job as `n` sequences against `max_num_seqs`, as vLLM
  does.

Records written before the next harmony-llm restart lack `n` and replay as `n=1`,
which undercounts chip traffic's batch slots.

**Hypothesis, not checked.** Two concurrent `n=12` chip calls take 24 of 32 batch
slots. That may be part of why the journal shows requests waiting below a full KV
cache (design §6), which the replay model could not reproduce. It can be checked
once records carry `n`.
