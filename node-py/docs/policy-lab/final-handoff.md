# Harmony routing policy lab handoff

Status: the offline lab and M0–M5 contract fixtures are implemented. Live routing was
not changed. Authorized profiling and a held-out measured replay were performed through
the durable workload authority, but the multi-vantage 27B/ASR/TTS domain remains
**unqualified** because only three of 378 profile cells are measured. No live shadow or
canary was performed.

The pinned incumbent adapter SHA-256 is
`182eec637bb8bd5c0ca49ae596d4675f608161ef10054e7ca88a86f7c892adc3`.
It remains the lab's active-policy reference; no release authorization or activation
artifact was created.

## Evidence by milestone

- M0: strict contracts, source/reuse decisions, test matrix, content-addressed storage,
  and CPU-only package tests.
- M1: deterministic simulator, S01–S32 fixtures, engine/network/resource models,
  policy sandbox and baselines. `docs/policy-lab/m1-benchmark-report.json` is explicitly
  synthetic-only and uncalibrated.
- M2: fail-open bounded emitters and consumer adapters. The pinned 20,000-sample
  `docs/policy-lab/observer-overhead/report.json` passes the overhead gate. The private
  integration evidence reports every recorder disabled and therefore
  `insufficient_evidence`; no missing caller is extrapolated away.
- M3: the private evidence directory contains the 378-cell / 37,800-request profiling
  plan, three admitted 100-sample warm profile packs, and a separately admitted
  20-episode/60-request held-out incumbent replay. Causal LLM→TTS arrivals are recomputed
  from simulated parent completion; historical waits are not predictions. Qualification
  remains `insufficient_evidence` with no certificate because cold, prefix-hit, higher
  concurrency, remaining shapes/windows and CA-vantage cells are unmeasured.
- M4: cycle manifests, durable-authority submission/idempotency, finite budgets,
  candidate isolation, independent scenario admission, and cycle CLI are implemented.
  The isolated fixture accepts a useful deterministic candidate, rejects an invalid
  network-import candidate, returns `no_change`, and preserves the incumbent.
- M5: read-only shadow, exact release binding/authorization, rollback fencing, and the
  future canary runbook are fixture-tested. This is not evidence that a shadow or canary
  ran.

## Offline commands

Run from `node-py`:

```bash
uv run --extra dev pytest -q tests/policy_lab
uv run python -m livestack_node.policy_lab validate livestack_node/policy_lab/fixtures/scenarios-v1.json
uv run python -m livestack_node.policy_lab replay-smoke livestack_node/policy_lab/fixtures/scenarios-v1.json
uv run python -m livestack_node.policy_lab observer-overhead --samples 20000 --warmup 1000 --out /tmp/policy-overhead
uv run python -m livestack_node.policy_lab completeness --manifest TRACE_MANIFEST.json --out /tmp/completeness
uv run python -m livestack_node.policy_lab profile-plan --manifest PROFILING_MANIFEST.json --out /tmp/profile-plan
uv run python -m livestack_node.policy_lab profile-submit --plan /tmp/profile-plan/report.json --authority-config AUTHORITY.json --handler policy_lab_profile --input-digest SHA256 --max-jobs 1
uv run python -m livestack_node.policy_lab profile-status JOB_ID --authority-config AUTHORITY.json
uv run python -m livestack_node.policy_lab calibrate --observations CALIBRATION_DATASET.json --profiles PERFORMANCE_PROFILES.json --out /tmp/calibration
uv run python -m livestack_node.policy_lab heldout-evaluate --dataset HELDOUT_REPLAY_DATASET.json --out /tmp/heldout
uv run python -m livestack_node.policy_lab cycle plan --config CYCLE_CONFIG.json --out /tmp/cycle-plan
uv run python -m livestack_node.policy_lab cycle fixture --seed 7 --out /tmp/cycle-fixture
```

`completeness`, `calibrate`, and `heldout-evaluate` intentionally return exit 4 when
evidence is insufficient. `profile-submit`, `profile-status`, `cycle submit`, `cycle
status`, and `cycle report` require an explicit workload-authority configuration.
Submission refusal is reported and never falls back to local execution. The held-out
evaluator resolves dependency arrivals from simulated completions, imports only observed
external occupancy, rejects historical wait/future-queue prediction fields, and
attributes completion-timing and state-transition mismatches separately.

## What is required next

Continue the admitted matrix until every claimed shape/cache/concurrency/path has the
frozen sample, window and cold-start minimums, then repeat independent calibration and
held-out replay. The installed workers refuse active-service interference and never fall
back locally. Only complete measured coverage can produce a domain certificate. A later
shadow/canary still requires separate release authorization under `canary-runbook.md`.
