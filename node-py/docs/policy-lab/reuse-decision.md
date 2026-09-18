# External and Harmony reuse decision

Assessment date: 2026-09-17. Versions are evaluation anchors, not dependencies.

| Candidate | Released anchor / license | Useful capability | Integration cost | Unsupported policy-lab requirements | Decision |
|---|---|---|---|---|---|
| NVIDIA Dynamo | 1.4.0 metadata on upstream `pyproject.toml`; Apache-2.0 | KV/load-aware LLM routing, engine telemetry, advisory planner, TTFT/ITL performance modelling | high: distributed runtime/Kubernetes and GPU-serving assumptions; Python >=3.10 | mixed ASR/TTS/workflow semantics, cross-region payload contracts, deterministic CPU-only counterfactual kernel, protected evaluator | optional LLM telemetry/profile adapter only; do not adopt as framework |
| Ray Serve | docs anchor 2.58.0; Apache-2.0 | multiplexed model affinity, LRU eviction, replica placement/autoscaling, broad runtime | high: Ray cluster/control plane and actor lifecycle | lab-specific causal replay, bounded regional delegation, speech coverage/playback, independent evaluator and release evidence | profile/telemetry adapter may be added for a Ray-served engine; no runtime dependency |
| llm-d Router | 0.10.0 release; Apache-2.0 | prefix-cache/load-aware LLM routing, priorities/flow control, standalone or Kubernetes gateway deployment | medium/high: Go proxy/EPP and LLM endpoint model | mixed engines/workflows, model-residency simulation, client-region payload paths, full offline evidence lifecycle | optional route-decision importer; no scheduler replacement |
| Existing Harmony | source `b0c2b78f`; repository license applies | host/fleet placement, leases, ledgers, demand history, durable workloads, regional mirrors | low: native data and execution authority already exist | deep queue/stream/network prediction, counterfactual state, calibration and protected evaluation | selected foundation: adapt incumbent policy and existing ledgers/workloads |

Selection: retain Harmony as the execution and durable-work framework. Build a
runtime-neutral, CPU-only lab kernel and thin optional adapters for measured
behavior. Dynamo is the strongest first optional adapter for LLM KV/performance
telemetry; Ray Serve and llm-d remain supported input sources when an engine is
actually deployed behind them. None is allowed to define benchmark truth or
silently narrow the ASR/TTS/workflow/cross-region contract.

Primary references:

- https://github.com/ai-dynamo/dynamo/blob/main/pyproject.toml
- https://docs.nvidia.com/dynamo/dev/knowledge-base/concepts/system-architecture/kv-aware-routing
- https://docs.nvidia.com/dynamo/dev/knowledge-base/modular-components/planner/overview
- https://docs.ray.io/en/latest/serve/model-multiplexing.html
- https://docs.ray.io/en/latest/serve/architecture.html
- https://github.com/ray-project/ray/blob/master/LICENSE
- https://github.com/llm-d/llm-d-router/releases
- https://github.com/llm-d/llm-d-router
