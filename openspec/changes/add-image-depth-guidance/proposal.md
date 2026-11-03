# Add image depth guidance

## Why

The managed image endpoint proves prompt/seed use but has no contract for a geometry guide. Histo's experiments make native Qwen depth ControlNet worth qualifying; forwarding an extra field would not prove consumption, and adding depth attributes to an existing worker kind could misroute planner grants.

**Design record realised:** `_plans/image-depth-guidance.md`. **Stale assumptions named there:** prompt/seed-only evidence is sufficient, text-to-image is the only routable image capability, and base-model residency is the full footprint. Those assumptions are stale for guided requests only.

## What Changes

- Add a bounded JSON `control_image` PNG payload and `controlnet_conditioning_scale`; guided requests require `depth_guide: true` and exact base/ControlNet revisions and config hash.
- Add optional worker configuration for a pinned ControlNet snapshot, with startup verification of its JSON config and weight, plus declared incremental residency.
- Use distinct unit kinds for depth/quantization variants so broker aggregation cannot merge them with ordinary Qwen workers.
- Construct `QwenImageControlNetPipeline` with shared base components inside the managed unit. Return and verify guide hash, scale, model/config revisions, control interval, and actual decoded output dimensions.
- Add a dedicated qualification lifecycle with isolated broker/gateway/worker processes and a credential-free dispatch handoff. No production peer or wrapper is used.
- Preserve ordinary text-to-image behavior and serial GPU execution. No request-time download, direct caller-selected model path, or parallel routing ledger is allowed.

## Non-goals

- RGB img2img, canny, soft-edge, pose, arbitrary ControlNet models, or automatic pipeline selection.
- Historical prompt/scene semantics, guide rendering, and historical-accuracy judgment; these belong to Histo.
- Production worker activation or deployment. Qualification uses only dedicated test processes.
- Deleting existing unguided behavior or introducing a parallel residency/routing ledger.

## Capabilities

### New Capabilities

- `image-depth-guidance`: validated depth-control requests, capability routing, immutable model/guide provenance, managed execution, isolated qualification, and failure behavior.

### Modified Capabilities

None.

## Impact

- `node-py/livestack_node/imagegen/contract.py`, `runtime.py`, `worker.py`, and `gateway.py`.
- `node-py/tests/test_imagegen_{contract,runtime,worker,gateway,qualify_depth}.py`, mixed-kind broker coverage, and focused real-model receipts.
- `node-py/examples/harmony-image/{depth-worker.example.json,qualify-depth.py,README.md}`.
- External artifact: `InstantX/Qwen-Image-ControlNet-Union` revision `b13036f066d6dee7c20513e263d3d673055e9de8`, config SHA-256 `6d80a5e2ab3abeb773b06f917b54a72f60c35ce91091f59f79243fb0f5d3d4bd`, weight SHA-256 `d51dca0073366a675108d5b83c3b7ef941cf2214c9a1c95c23f1e9a228ddbdb0`, Apache-2.0.
- Companion consumer change: `/home/ubuntu/histo/openspec/changes/qualify-grounded-depth-images`.
