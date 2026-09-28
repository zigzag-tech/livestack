# First production run — xc-tower-ubuntu, 2026-09-28

## Deploy (tasks 2.3, 3.3)

- `harmony-llm` restarted at 04:58 UTC on livestack `main` `f19ccaf3`, with drop-in
  `85-composition.conf` (`HARMONY_DEMAND_LOG_AGE_DAYS=21`, `HARMONY_KV_DTYPES=auto,fp8`)
  and `lora_base: Qwen/Qwen3.8-27B` added to `llm_general` (backup
  `/etc/harmony/llm-units.json.bak-20260928T045723Z-pre-lora-base`). Ready in 160 s.
- Journal: `llm_general: measured 23.51 GiB (weights 18.39, activation 2.56, KV 1.66 =
  37981 tokens, graphs 0.90) for sha256:ef0458d1b1f3…`
- `/residence`: `footprint {"vram_bytes": 25243670282}`, `footprint_source: vllm-startup`,
  was the declared 21 GB. **Reverted the same hour** (design §8b): with the broker's 2 GB
  device reserve, the card's 25.30e9 B left about 23.3e9 B, so a reload of `llm_general`
  would have been unplaceable. `footprint` is back to the declared prior, and the
  measurement (including `min_footprint`) is reported beside it.
- Demand log live at `~/.cache/livestack/demand/xc-tower-ubuntu-gpu1.jsonl`, first
  records with real token counts (e.g. 1,612 prompt / 55 completion, 9.1 s).
  `owner_ns` is `null` on current traffic: the hub still sends unprefixed owners
  (pre-R.4), which the log reports as unknown rather than guessing.
- Not done: the 24 h count check of task 3.3 (records vs `vllm:request_success_total`).

## Backfill

The chips-only/bf16 composition ran before measurement existed. Its vLLM startup
lines (journal 2026-09-27 22:10:49–22:11:46, pid 1004693, the same text as
`tests/fixtures/vllm_startup/v0.28.0-llm_general-bf16.log`) were parsed by
`vllm_startup.parse` and stored in `unit-costs.jsonl` with a `backfilled_from` field.
That leaves two measured rows on this host.

## Runs

| decision | outcome | note |
|---|---|---|
| `01M3K65FS7Z49M835XV9NX7BKV` | keep | 1 measured row: 45/48 candidates `unknown:no_measured_basis` |
| `01M3K65X9C5PSC2JFFR4G87XDP` | keep | **bug:** capacity from nvidia-smi (24.0 GiB), not the engine's 23.56; bf16 two-adapter rated feasible at 28,133 tokens. Fixed in `59ba52f6` |
| `01M3K67295PW3FQQFZ0VH5NX6Z` | keep | capacity fixed (bf16 two-adapter 22,480 tokens, infeasible); **bug:** record over 32 KiB, live row shed by the writer |
| `01M3K69CF69E7TF5EVW49YSCFX` | keep | clean: not truncated, live row kept, 2 rows omitted for size (counted), `--replay` reproduced |

The last run is the reference. It keeps the live chips+jemm/fp8 composition,
because no candidate beats it by more than the change cost (4.9 request-seconds on
current, light traffic). Its record lists, among others:

- chips+jemm/bf16 at 24,576: `filtered:infeasible:kv_tokens<max_model_len`
  (22,480 predicted). This is the 2026-09-28 fact that previously lived only in a
  person's head.
- chips-only/bf16 at 24,576: 29,748 predicted vs 29,749 measured.
- every `max_num_seqs=16` candidate: `unknown:no_measured_basis:max_num_seqs`. No
  batch cap other than 32 has ever been measured here.

The two earlier records with bugs stay in the ledger as they were written.
