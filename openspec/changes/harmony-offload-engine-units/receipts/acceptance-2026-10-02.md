# Acceptance receipts — harmony-offload-engine-units (2026-10-02, xc-tower-ubuntu)

Everything below is request-driven. Nothing called `/model/warm`, `POST
/model/warm` or started an engine by hand: residency was caused only by
requests written in the request language (`harmony_requires`, broad
characteristics). `strata.sh verify` is verification of an engine the request
path had already loaded.

## The install (task 6.1-6.2)

- disk: `diskreap auto --force` freed 65.5 G (target 100 G free; 123 G after).
  NOTE: its `remove-worktree` category deleted six stale benchday worktrees —
  the tool's safety model (clean, untracked, unreferenced), named here so a
  sibling agent knows where they went.
- rev pinned: `architectds/Strata` tag `v0.1.24-linux-cuda12.8`
  (`e416ba57f589f7c2561a1ed14ef72d4358f14027` in `~/strata/strata/STRATA_VERSION`)
  with the fork's own prebuilt engine (`strata-linux-x64.zip`: 0.1.24 for
  sm_80/sm_86/sm_89, CUDA 12.8). The design-surveyed rev `36fa455` cannot
  install here: its engine must compile and the build dies against this host's
  glibc 2.4x + CUDA 12.9 (`cospi` exception-specification conflict; recorded in
  `_plans/harmony-engine-units.md` Q1).
- model: `Qwen3.8-Flash-Next-GSQ-RCO-Q2_0` (37.62 + 28.80 GB shards,
  `~/strata/Strata-data/models/Q2_0/`, pack `packs/q2_0`, MTP `mtp/rt`).
  Setup measured: experts loaded 31.64 GiB of RAM at 1.67 GiB/s, expert cache
  13514 experts / 17.40 GiB of VRAM, load ≈ 39 s from request to ready.
- one card: `--gpu 1` (the setup's default was a [0,1] layer split, which the
  exclusive one-card claim forbids); `--context 131072`, `--vision none`.

## (a) Long-context request loads Flash-Next by evicting the idle 27B — PASS

Request (the spec's spelling, list clause):
`{"model": "require:class=llm,context_len=[131072,]", "harmony_requires":
{"class": "llm", "context_len": [131072]}, ...}`

Journal (`journalctl -u harmony-llm`):
```
20:55:44 [harmony-llm] {'class': 'llm', 'context_len': [131072]} -> flash_next (request named llm_general)
20:55:44 [harmony-llm] stopping vLLM
20:55:45 [harmony] evicted llm_general (resident=[])
20:55:45 [harmony-llm] starting strata for flash_next: ... serve/server.py --engine strata --config strata-q2_0.json --port 8191 --host 127.0.0.1 --gpu 1
```
Response (`/tmp/accept-a6.json`):
```
{"id": "chatcmpl-921066e868914e76aeea2776", "model": "qwen3.8-flash-next-q2_0",
 "choices": [{"message": {"role": "assistant", "content": null,
   "reasoning_content": "We need to respond to user ..."}, "finish_reason": "length"}],
 "usage": {"prompt_tokens": 62, "completion_tokens": 24, "total_tokens": 86},
 "timings": {"prompt_n": 62, "prompt_ms": 1598.2, "prompt_per_second": 38.8,
             "predicted_n": 24, "predicted_ms": 938.8}}
```
So: the 27B evicted, Flash-Next loaded through Strata, answered from it — in
one request, no resubmission. Load time request-to-ready ≈ 39 s; prefill
38.8 tok/s, decode ≈ 25.6 tok/s (24 tokens / 938.8 ms) on Q2_0 + MTP.

The eviction here was the node's manager honouring the unit's OWN
`exclusive_device` claim (the broker's snapshot had the node `suspect` — its
facade blocks while it serves — so the request took the named
`broker temporarily forgot` fallback). Both paths are Harmony; the fallback's
room-making is `coordinator.acquire`'s exclusive-override-coload (landed
36a4d929) after the acceptance caught it loading onto a full card (399 MiB
free, cudaMalloc failed).

## (d) `model: "local"` reaches the 27B, never Flash-Next — PASS (after fix)

The acceptance found the alias resolving as "no opinion" (the reuse shortcut
would have served the resident flash_next). Fixed: the legacy alias is a
CHOICE (`_model_choice`) resolved to the default unit, while `_named_unit`
stays strict ("no opinion must be tellable" — `test_naming_nothing_is_not_naming_the_first_one`
keeps its pin). See `tests/test_requirement_lists.py`.

## 5.2 API shape on the pinned rev — PASS (`strata.sh verify`, exit 0)

```
[ok] GET /health: status 200
[ok] GET /v1/models: status 200
[ok] tool_calls: [{"id": "call_da20081266dc4f60b7ebaf23", "type": "function",
                   "function": {"name": "get_weather", "arguments": "{\"city\"...
[ok] usage: {"prompt_tokens": 55, "completion_tokens": 26, "total_tokens": 81, ...}
[ok] stream_options.include_usage: data: {"id": "chatcmpl-fcc6c401...", ...
[ok] process on the port: /home/ubuntu/strata/strata/.venv/bin/python
     /home/ubuntu/strata/strata/serve/server.py --engine strata ...
strata rev: e416ba57f589f7c2561a1ed14ef72d4358f14027
```
The process name is `serve/server.py --engine strata` — the pinned rev has no
`llama-server` binary (design §1's sketch); the adapter drives the real entry
point. Reasoning is returned SEPARATELY (`reasoning_content`) and tools work
(`tool_calls`), so `thinking: true` / `tools: true` are the honest attributes;
`/proc` pin field is `VmLck` (positive control in
`tests/test_engines_strata.py`).

## What the acceptance broke and the fixes it forced (all landed)

1. `serve._footprint_signature` int()-crashed on a vector footprint — the node
   would not boot (36a4d929 preceded it: `c4a120c1`).
2. A list clause (`context_len=[131072,]`) satisfied NOTHING — neither matcher
   implemented "a value in this list" (`1e076011`).
3. `HARMONY_LLM_COLOAD=0` (single-model era) made every `ensure()` evict the
   node's siblings, stopping a just-loaded Flash-Next mid-request (disabled in
   the service; `80-coload.conf.disabled-20261002`).
4. The local fallback's room-making was coload's job — with multi-unit coload
   it loaded onto a full card. `exclusive_device` now overrides coload
   (`36a4d929`).
5. `model: "local"` was "no opinion" and lost to the reuse shortcut (`e4a94948`
   lineage; see (d)).
6. The 502 carried an empty message; it now names the exception type.
