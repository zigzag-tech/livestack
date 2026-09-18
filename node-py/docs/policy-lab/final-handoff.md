# Harmony routing policy lab handoff

Status: the offline lab and M0–M5 contract fixtures are implemented. Live routing was
not changed. The two-region 27B/ASR/TTS domain is **unqualified** because no authorized
profiling principal/handler or admitted measurement pack was available. No profiling
job, held-out measured replay, live shadow, or canary was performed.

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
  plan and an empty measured profile pack. Calibration correctly returns exit 4,
  `insufficient_evidence`, with no certificate. Authorized measurement and held-out
  incumbent replay remain unperformed.
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
uv run python -m livestack_node.policy_lab calibrate --observations CALIBRATION_DATASET.json --profiles PERFORMANCE_PROFILES.json --out /tmp/calibration
uv run python -m livestack_node.policy_lab cycle plan --config CYCLE_CONFIG.json --out /tmp/cycle-plan
uv run python -m livestack_node.policy_lab cycle fixture --seed 7 --out /tmp/cycle-fixture
```

`completeness` and `calibrate` intentionally return exit 4 when evidence is
insufficient. `cycle submit`, `cycle status`, and `cycle report` additionally require an
explicit workload-authority configuration and never fall back to local execution.

## What is required next

Provision a specifically authorized profiling handler/principal, replace every
`*-revision-required` placeholder with observed immutable revisions, admit the bounded
profiling plan, collect at least the frozen sample/window/cold-start minimums, and run
held-out incumbent replays with causal arrivals and observed external occupancy. Only
then can calibration produce a domain certificate. A later shadow/canary still requires
separate release authorization under `canary-runbook.md`.
