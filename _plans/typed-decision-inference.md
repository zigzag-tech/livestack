# Typed-decision inference through Harmony

**Status:** closed — plumbing shipped; portable profile **not** complete.
Livestack owns the generic cross-architecture decision service, admission,
physical CUDA/MLX kinds, and worker adapters. Benchday owns the consuming
attention/chip contracts and OpenSpec archive. **Neither repository archives
the other.** The frozen contract revision is
`typed-decision-contract-v1.0.0` (`schema_version: benchday.decision.v1`).

Linked Benchday change:
`openspec/changes/route-pane-decisions-through-harmony` (same revision file).
The placement-ledger schema at `livestack_node/decision.schema.json` is **not**
this API.

## Why

Harmony today can grant GPU residency for chat/embed/speech. It cannot:

1. Serve a versioned typed-decision profile (`choice` / `noul`) on equivalent
   CUDA and MLX Laya implementations.
2. Require authenticated, scoped admission for every new decision request.
   The existing `/admit` path degrades to `granted: True` when planning raises —
   the opposite of what decision traffic needs.
3. Keep CUDA and MLX physically distinct while sharing one logical profile.

## Contract

Shared JSON lives in this repo at
`node-py/livestack_node/decisions/data/` and in Benchday at
`contracts/typed-decision-v1/`. Hashes must match. Packing version is
`decision-pack-v1`. Default Benchday policy stays `legacy` until qualification.

Physical kinds (not interchangeable with each other as kinds):

- `laya_multilingual_cuda_v1`
- `laya_multilingual_mlx_v1`

Logical profile: `pane-attention-v1:unqualified` / `pane-chips-v1:unqualified`
until Q01–Q04 and both hardware reports exist. Architecture-free callers name
the profile only. Mismatched language/calibration/limits refuse.

## Gaps this plan closes

- Authenticated `POST /v1/decisions` with realm/owner headers derived from
  `fleet_auth` principals. Body `account_id` is never trusted.
- Decision-specific admission that **refuses** on broker loss for cold *and*
  warm new requests. Already-admitted in-flight work may finish. The legacy
  unauthenticated `/admit` degrade-to-grant path is not a bypass.
- Distinct CUDA/MLX kinds, same-kind descriptor conflict detection, measured
  memory envelopes (unknown footprint ≠ 0 bytes).
- Host-pressure-aware MLX accounting without CPU/GPU double counting.
- Bounded queue, 64 KiB bodies, microbatch ≤8, deadlines, attempt ids,
  cancellation accounting.
- Fixture backend for H01/H03–H08/H10. Real adapters for H08/H09.

## Packets (same letters as the Benchday delegation)

| Packet | Owner here | Acceptance |
|---|---|---|
| A | contract data + extras pin | H02/H03/C05 fixtures |
| B | `decisions/` service, admission, kinds | H01, H03–H08, H10 |
| C | CUDA Laya adapter | H02–H05/H08/H09 on NVIDIA |
| D | native MLX adapter | H02–H05/H08/H09 on Apple Silicon |
| I | qualification reports | no-go unless both backends + Q gates |

Packets E–H are Benchday consumers. They must not implement scoring inside the
broker.

## Non-goals

- OpenSpec init in this repository.
- Claiming Benchday OpenSpec archives Harmony scheduler truth.
- Cloud fallback, CPU fallback on a CUDA/MLX qualified worker, or substituting
  chat/embed units for a decision profile.
- Enabling serve because plumbing imported.

## Completion

Closed 2026-09-20 with the portable profile explicitly **not** complete:

- Contract `typed-decision-contract-v1.0.0` unchanged. pytest for the
  decision ingress is green (`test_decision_contract.py`,
  `test_decision_service.py`, `test_upstream_map.py`).
- Real CUDA (`convaiinnovations/laya-multilingual` on RTX 3090) and native
  MLX (`aac6fef/laya-multilingual-mlx` on xc-mac-studio arm64) H09 reports
  exist. Short-status argmax agrees (`question`); P(question) Δ=0.079 and
  max-context labels disagree (`working` vs `finished_turn`). Tolerance
  0.01 is not met. Profile ids stay `*:unqualified`.
- Q01–Q04 sealed datasets were not scored (insufficient-bucket). Do not
  enable serve.
- Benchday hub plumbing shipped hub-only (train
  20260920T074509Z-2900416). Linked OpenSpec change remains unarchived
  until a later enablement qualifies. Missing gates stay open.
