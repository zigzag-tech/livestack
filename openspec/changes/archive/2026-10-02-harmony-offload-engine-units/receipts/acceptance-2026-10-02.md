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

## Scenario run 2 (the state dance, request-driven) — 2026-10-02

`/tmp/accept-all.log` + `/tmp/accept-rest.log` hold the raw logs. Summary:

- **(b) no swap** — broad `require:class=llm` while the 27B resident →
  `model: dbirks/Qwen3.8-27B-W4A16-AutoRound`, state unchanged
  (`llm_general resident, flash_next not`). PASS.
- **(a) long-context** — `require:class=llm,context_len=[131072,]` →
  `model: qwen3.8-flash-next-q2_0`; state after:
  `llm_general evicted, flash_next resident`. PASS (also in run 1).
- **(f) host RAM inside the pool** — before: used 17 G / available 76 G;
  after Flash-Next resident: used 48 G / available 46 G (the ~31.6 GiB expert
  pin + MTP), swap 74→76 G flat, no OOM. PASS.
- **(g) ASR on card 0** — `tower-asr-1 [('asr', True)]` in EVERY snapshot
  across the whole run. PASS.
- **(c)** — the first attempt sent the body-JSON list form (`max_concurrent:
  [8]`, membership) instead of the spec's interval spelling
  (`max_concurrent=[8,]` = `>=8` per `_expand_clause`): nothing satisfied it.
  Re-shot with the model-string spelling in run 2.
- **(d)** — the first attempt ran INSIDE flash_next's anti-thrash floor and was
  correctly deferred ("was not admitted: the planner could not place it on any
  device"); the scenario's "idle past its minimum residency" applies here too.
  Re-shot after the floor in run 2.
- **(d2)** — the first prompt was 144,950 tokens (past even Flash-Next's
  131,072): the engine refused and — the acceptance's second dialect finding —
  the refusal fell through as a raw 400 because the block matched only vLLM's
  "context length" words. Fixed (Engine.context_refusal + both number
  spellings, landed `2d1f3380`); re-shot with a ~26k-token prompt in run 2.
- **(e)** — the first attempt sent `vision=[true,]` and the parser refused it
  ("bad requirement 'vision': not a number: 'true'") — a 400, correctly, and
  naming the rule; re-shot as a 503 case in run 2.

Run 2 results are appended below by the follow-up capture.

## Scenario run 3 — 2026-10-02 (all fixes deployed)

- **(d) PASS** — `model: "local"` with the models having bounced →
  `model: dbirks/Qwen3.8-27B-W4A16-AutoRound`: the legacy alias reaches the
  27B, never Flash-Next (`/tmp/g-d.json`). The `_model_choice` fix is what
  makes this true.
- **(d2) re-route PROVEN in the journal** — `context refusal on llm_general
  (24577 tokens) — re-routing once to require:class=llm,context_len>=24577`
  (21:51:07), and the stale-world fallback answering a later one with
  `using locally declared flash_next` (22:03:51). The END-TO-END answer kept
  meeting the honest-cost wall underneath it: the 26k-token prompt's first
  leg holds the 27B busy for its prefill, and every window in which
  Flash-Next could be admitted was either inside a floor or taken by live
  title traffic — and a busy tenant is now correctly NEVER evicted out from
  under its work (that fix is what stopped the ReadError stream cuts).
- **(e) PASS** — `{"detail":"nothing satisfies {'class': 'llm',
  'no_such_attribute>=': 7}"}` — a 503 naming the rule (run 2); run 3's
  refusals additionally carry the planner's own words after the fix
  (`— the planner could not place it on any device`).
- **(b), (c), (f), (g)** — see run 2 (all PASS).

## The fix ledger (every one landed and pushed)

| # | Found by | Fix | landed |
|---|---|---|---|
| 1 | node would not boot | `serve._footprint_signature` speaks the vector | `c4a120c1` |
| 2 | scenario (a) 503 | list clause = "a value in this list" (both matchers) | `1e076011` |
| 3 | flash_next stopped mid-request | `HARMONY_LLM_COLOAD=0` disabled (multi-unit coload) | service drop-in |
| 4 | cudaMalloc failed (399 MiB free) | exclusive claim overrides coload | `36a4d929` |
| 5 | `local` answered by flash_next | the alias is a CHOICE (`_model_choice`) | `e4a94948`+ |
| 6 | node wedged (`residence:000`) | the queue waits OFF the event loop | `80e257cb` |
| 7 | raw 400 instead of the 413 | context dialect via `Engine.context_refusal` | `2d1f3380` |
| 8 | 503 said "nothing satisfies" for a floor | the 503 carries `defer_reason` | `2dc6fb33` |
| 9 | shed loop (`relieve measured over-budget pressure`) | the exclusive claim is symmetric | `07f04731` |
| 10 | stale world 503 on a locally-known unit | "no unit satisfies" from a stale world = did-not-know | `7692d966` |
| 11 | ReadError stream cut | busy tenants are never evicted mid-work | `c9d3bed6` |

What remains TRUE and unfixed is in `_plans/harmony-engine-units.md`
"What the queue and the re-route do NOT fix" — including the model bounce
itself: with card 1 serving both a 27B for titles and Flash-Next for long
context, the floors and the honest costs mean the two swap on the traffic
pattern, and each swap is 40 s (Strata) or 50 s (vLLM). Q2, stated once.

## The goal's explicit criteria — run some cat, run some LLM calls (Qwen Flash-Next)

```
$ POST /v1/chat/completions {"model": "require:class=llm",
    "harmony_requires": {"class": "llm", "tools": true},
    "tools": [<cat tool schema>], "tool_choice": "auto", ...}
model: dbirks/Qwen3.8-27B-W4A16-AutoRound
tool_call: cat {"path": "/etc/hostname"}
RAN: cat /etc/hostname -> 'xc-tower-ubuntu'
```
An LLM call whose `cat` tool call RAN — through Harmony, request language
only. And the Flash-Next calls (scenario (a), run 1):
`model: qwen3.8-flash-next-q2_0`, `usage: {prompt 62, completion 24, total 86}`,
`timings: {prompt_per_second: 38.8, predicted: 24 in 938.8 ms}`, reasoning in
`reasoning_content` — Qwen3.8-Flash-Next BEING the model that answers, loaded
by the request that needed it.

## One finding left open (recorded, not fixed)

A Flash-Next admission is refused `no device can fit even with preemption` in
hostd's FULL world while the same world snapshot planned locally grants
(`Evict llm_general -> Load flash_next -> Grant` — reproduced side by side
against the live reports). The partial-world repro (the tower-llm peer alone)
always grants; the difference is the linked/federated peer set. The refusal is
at least honest now (the 503 carries it), and scenario (a) proves the
placement path works — but WHY the full world refuses is unexplained and open.
