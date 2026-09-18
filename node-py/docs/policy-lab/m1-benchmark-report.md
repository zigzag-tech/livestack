# Harmony policy lab M1 synthetic benchmark

Evidence level: **synthetic-only**. Qualification: **none**.

All S01-S32 fixtures have independent mechanics/invariant tests against the
explicit analytic profiles. The comparison surface includes the pinned current
Harmony adapter, nearest-ready, warm-first, least-predicted-queue, and the
total-latency/demand-aware experimental policy.

This report is uncalibrated. It makes no claim about real two-region latency,
throughput, memory accuracy, live routing readiness, shadow counterfactual
performance, or canary safety. Unmeasured engine colocation remains
unsupported. Calibration and live evidence require later admitted tasks.

Inputs:

- Scenario catalog SHA-256: `bb64fa6dba7dfaaac74e1b2c719b50bf1770d3da69d511d7277ee6b5dd3bbfc9`
- Synthetic profile SHA-256: `778ef8af3006367b92737bd6ade07a77e28d46e8464e0f6d3641a5246917b22c`

Reproduce:

```bash
cd node-py
python -m pytest -q tests/policy_lab
python -m livestack_node.policy_lab validate livestack_node/policy_lab/fixtures/scenarios-v1.json
```
