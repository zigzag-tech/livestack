# Requirements-to-test matrix

This matrix assigns every normative capability and S01-S32 to an independent
test owner/location. A listed test is planned until its task is checked; names
are stable targets, not evidence that the test already exists.

## Capability requirements

| Capability | Requirement / normative scenario | Owner and test location |
|---|---|---|
| observation | logical requests vs attempts / retry popularity | contracts; `tests/policy_lab/test_observation_join.py` |
| observation | rejected alternatives and outcomes / missing completion | import; `tests/policy_lab/test_observation_import.py` |
| observation | explicit time and causality / clock skew | import; `tests/policy_lab/test_causality.py` |
| observation | private bounded evidence / full spool | emitter; `tests/policy_lab/test_emitter.py` |
| observation | all offered history / censored timeout | import+metrics; `tests/policy_lab/test_offered_load.py` |
| profiles | conditional versioned profiles / unmeasured colocation | profiles; `tests/policy_lab/test_profiles.py` |
| profiles | independent calibration / absent cold samples | calibration; `tests/policy_lab/test_calibration.py` |
| profiles | no implicit profiling / unknown load time | CLI; `tests/policy_lab/test_cli_offline.py` |
| profiles | drift invalidation / engine upgrade | profiles; `tests/policy_lab/test_profile_drift.py` |
| simulation | causal counterfactual replay / early predecessor; determinism | kernel+DAG; `tests/policy_lab/test_simulation.py` |
| simulation | conserved ownership / concurrent cold requests | resources; `tests/policy_lab/test_resources.py` |
| simulation | path contention / upload and TTS | network; `tests/policy_lab/test_network.py` |
| simulation | streaming semantics / TTS underrun | engines; `tests/policy_lab/test_streaming.py` |
| simulation | visible unsupported/unfinished work / overload | reports; `tests/policy_lab/test_horizons.py` |
| policy evaluation | bounded proposals / active-lease eviction | validator+sandbox; `tests/policy_lab/test_policy_contract.py` |
| policy evaluation | five comparable baselines / cold costs | policies; `tests/policy_lab/test_baselines.py` |
| policy evaluation | immutable partitions / bundled evaluator | datasets+packaging; `tests/policy_lab/test_dataset_integrity.py` |
| policy evaluation | no hidden subgroup regression / Canadian dictation | metrics+gates; `tests/policy_lab/test_promotion.py` |
| policy evaluation | reviewed incident expansion / duplicate mechanism | curator; `tests/policy_lab/test_scenario_admission.py` |
| improvement cycle | reproducible durable artifacts / disconnect | cycle+workloads; `tests/policy_lab/test_cycle.py` |
| improvement cycle | bounded isolation / infinite loop | sandbox; `tests/policy_lab/test_sandbox.py` |
| improvement cycle | no-change outcome / all candidates regress | cycle; `tests/policy_lab/test_cycle_outcomes.py` |
| improvement cycle | scoped independent release / offline pass | release; `tests/policy_lab/test_release_state.py` |
| improvement cycle | interference-aware canary / rollback streams | release fixtures; `tests/policy_lab/test_rollback.py` |
| regional contract | independent control/payload / remote coordinator | network; `tests/policy_lab/test_regional_paths.py` |
| regional contract | permissions vs proximity / travelling client | eligibility; `tests/policy_lab/test_region_permissions.py` |
| regional contract | bounded delegation / duplicate regional grant | resources; `tests/policy_lab/test_delegation.py` |
| regional contract | semantic compatibility / incompatible TTS voice | validator; `tests/policy_lab/test_capabilities.py` |
| regional contract | explicit/private integration / public export | export; `tests/policy_lab/test_public_export.py` |

## Required scenario catalog

| Scenarios | Independent assertion owner / location |
|---|---|
| S01-S06 | routing/residency costs, decay, fairness and churn; `tests/policy_lab/scenarios/test_routing.py` |
| S07-S08 | shared weights, request memory, LLM cache/batching/KV; `tests/policy_lab/scenarios/test_llm.py` |
| S09 | accepted ASR coverage and backlog; `tests/policy_lab/scenarios/test_asr.py` |
| S10-S11 | TTS overlap, audible buffer, cancellation and cleanup; `tests/policy_lab/scenarios/test_tts.py` |
| S12-S13 | bottleneck conservation and control/data split; `tests/policy_lab/scenarios/test_network.py` |
| S14-S15 | delegation fencing and region constraints; `tests/policy_lab/scenarios/test_regions.py` |
| S16-S18 | load failure, stale telemetry, worker loss/restart; `tests/policy_lab/scenarios/test_failures.py` |
| S19 | DAG completion and artifact locality; `tests/policy_lab/scenarios/test_workflows.py` |
| S20 | retry demand deduplication and attempt costs; `tests/policy_lab/scenarios/test_observation.py` |
| S21 | voice/embedding/model compatibility; `tests/policy_lab/scenarios/test_capabilities.py` |
| S22-S23 | bounded observation, gaps, clock/order/conflicts; `tests/policy_lab/scenarios/test_observation.py` |
| S24 | arbitrary regions, heterogeneous GPU and alias conservation; `tests/policy_lab/scenarios/test_topology.py` |
| S25 | unsupported colocation blocks qualification; `tests/policy_lab/scenarios/test_profiles.py` |
| S26-S27 | reject-all/future oracle and partition leakage; `tests/policy_lab/scenarios/test_evaluator.py` |
| S28-S31 | timestamp boundary, illegal eviction, determinism, overload; `tests/policy_lab/scenarios/test_kernel.py` |
| S32 | profile drift and requalification; `tests/policy_lab/scenarios/test_profiles.py` |

The fixture catalog will contain per-ID manifests and oracle assertions; tests
must assert conservation/state/SLO invariants rather than snapshot a candidate's
chosen host.
