# Image depth guidance specification

## ADDED Requirements

### Requirement: Bounded JSON depth request contract

The ingress and worker SHALL preserve `control_image` as a JSON object containing strict-base64 PNG bytes and lowercase SHA-256. Decoded bytes/PIL SHALL be transient only. PNG bytes SHALL be at most 8 MiB, match the hash, decode as one RGB-readable image, and equal requested output dimensions. Scale SHALL be a non-boolean finite number from 0.8 through 1.0 inclusive. `depth_guide` SHALL be literal JSON true, and task SHALL be `text_to_image` whether supplied explicitly or defaulted.

#### Scenario: Valid depth request

- **WHEN** a caller submits a 768 × 432 request with a valid PNG of those dimensions, exact hash, scale 0.8, and `depth_guide: true`
- **THEN** validation preserves the JSON image object, prompt, scale, dimensions, and requirements for JSON forwarding

#### Scenario: Qwen native wide dimensions

- **WHEN** a caller submits a 1664 × 928 request and the selected worker's configured maximum permits it
- **THEN** the request contract accepts those dimensions and the runtime still verifies the actual output size

#### Scenario: Invalid guide or scalar is refused before inference

- **WHEN** hash/dimensions differ, base64 is invalid, payload exceeds 8 MiB, scale is absent/outside range/boolean, `depth_guide` is numeric 1, or task/class is wrong
- **THEN** validation refuses before planner admission or model execution

#### Scenario: Guide and requirement must be paired

- **WHEN** a request has an image without `depth_guide: true`, that requirement without an image, or scale without an image
- **THEN** validation refuses before admission

### Requirement: Distinct capability and variant routing

Guided requirements SHALL name exact base model/revision, ControlNet model/revision/config hash, and quantization. Depth workers SHALL advertise those attributes plus `depth_guide: true`; unconfigured workers SHALL omit depth attributes. Every depth/quantization/footprint variant SHALL use a distinct unit kind from coexisting variants. Qualification SHALL also require the pinned base revision for text baselines.

#### Scenario: Guided request cannot fall back

- **WHEN** available workers omit depth capability or advertise a different base/ControlNet/config revision
- **THEN** planner resolution refuses and no text-only substitute is returned

#### Scenario: Mixed worker variants cannot be merged

- **WHEN** an unconfigured Qwen worker and a depth worker register on one broker
- **THEN** their unit kinds differ, both retain their own attributes/footprints, and guided resolution selects only the depth kind

#### Scenario: Unguided request remains valid

- **WHEN** a caller submits no image, scale, or depth requirement
- **THEN** existing text-to-image routing and execution continue, including on a depth-capable unit in the isolated fleet

### Requirement: Immutable startup verification

Depth configuration SHALL name model, full revision, local snapshot, config hash, weight byte count, and weight hash. Before `attach` or advertisement, startup SHALL require Qwen/Qwen-Image-2512, base snapshot basename equal to base revision, the exact pair `qwen_image_2512_depth_nf4` with `nf4` or `qwen_image_2512_depth_int8` with `int8`, ControlNet snapshot basename equal to its revision, `config.json` class `QwenImageControlNetModel` with matching hash, and a weight with matching size/hash. Runtime construction SHALL repeat the exact-pair check. No model-hub access or request-time download SHALL occur.

#### Scenario: Verified snapshot enables capability

- **WHEN** base model/revision/snapshot, an exact unit/quantization pair, and both ControlNet config/weight files satisfy configured identities
- **THEN** the worker may advertise depth; a single-device initial footprint is base resident bytes plus full weight bytes for BF16 ControlNet or half the weight bytes, rounded up, for INT8 ControlNet

#### Scenario: Split execution preserves device and residual placement

- **WHEN** ordered `device_ids` and `CUDA_VISIBLE_DEVICES` map multiple physical GPUs, auto transformer budgets span logical GPUs, and sequential ControlNet runs on a configured logical device
- **THEN** only primary `resident_bytes` is charged to the primary placement, every device has its own lease and measured memory, and per-block hooks move each residual to the transformer block output device before addition

#### Scenario: Missing or changed startup input fails closed

- **WHEN** the base is not Qwen, base snapshot basename differs from base revision, unit and quantization are not an exact approved pair, snapshot/config is missing/malformed/changed, config class is wrong, or weight size/hash differs
- **THEN** startup fails with a typed configuration error and `attach` is never called

#### Scenario: Crossed unit and quantization are refused

- **WHEN** a config pairs `qwen_image_2512_depth_nf4` with `int8` or `qwen_image_2512_depth_int8` with `nf4`
- **THEN** startup and defensive runtime construction reject it before advertisement or model loading rather than merging variants

#### Scenario: Unsupported quantization cannot be relabeled

- **WHEN** a depth config declares `int4` or any quantization other than `nf4`/`int8`
- **THEN** startup and defensive runtime construction reject it before advertisement or model loading rather than producing NF4 output labeled `int4`

### Requirement: Managed shared-component execution and release

Depth runtime SHALL load the local ControlNet in configured BF16 or INT8 precision and directly construct `QwenImageControlNetPipeline` with the text pipeline's scheduler, VAE, text encoder, tokenizer, transformer, and loaded ControlNet. Guided generation SHALL pass exact guide/dimensions/scale/0–1 interval/seed/steps. It SHALL NOT create an img2img pipeline or second base model. Close SHALL clear all new references before garbage collection/cache clearing.

#### Scenario: Correct guided pipeline call

- **WHEN** a validated guided request reaches the runtime
- **THEN** the pipeline receives every required argument with shared components and actual output passes blank-image and exact-dimension guards

#### Scenario: INT8 transformer uses sequential CPU offload

- **WHEN** an isolated config declares `offload: {"strategy":"sequential","controlnet_gib":null}`
- **THEN** the text pipeline calls `enable_sequential_cpu_offload()`, INT8 CPU-dispatched modules use `llm_int8_enable_fp32_cpu_offload=True`, ControlNet remains GPU-resident without an unsupported automatic map, and managed evidence matches the configuration

#### Scenario: Runtime without depth support refuses guidance

- **WHEN** a guided request reaches an unconfigured runtime
- **THEN** it raises unsupported guidance and never invokes the text pipeline

#### Scenario: Incorrect actual output dimensions are refused

- **WHEN** a pipeline returns dimensions other than requested, including model-default 1024 × 1024
- **THEN** the attempt fails and no success receipt is emitted

#### Scenario: Eviction releases depth allocations

- **WHEN** a depth unit is freed and reloaded
- **THEN** text/depth pipelines and standalone ControlNet references are cleared before cache release and no stale depth runtime is reused

### Requirement: Guide-consumption and actual-output evidence

Guided results SHALL include `harmony.depth_guide` with actual decoded image hash, scale, ControlNet model/revision/config hash, and 0.0–1.0 interval. Worker `harmony.width` and `height` SHALL come from actual `image.size`, and `generation_settings` SHALL include steps, true CFG scale, negative-prompt semantics, text encoder, and max sequence length. Depth workers SHALL advertise those settings for gateway verification; legacy unguided fixtures SHALL NOT require them. Unguided results SHALL report depth guidance as null without requiring legacy unguided test doubles to change.

#### Scenario: Gateway accepts matching evidence

- **WHEN** worker result matches request guide/base/ControlNet/config/scale/interval/dimensions, the selected unit's advertised generation settings, and grant kind/device
- **THEN** gateway returns the result with fleet-grant evidence

#### Scenario: Missing or mismatched evidence fails

- **WHEN** worker omits depth evidence or changes guide hash, scale, base/ControlNet identity, config hash, either interval endpoint, actual width/height, steps, CFG, prompt-encoding settings, unit, or device
- **THEN** gateway fails rather than returning verified guidance

### Requirement: Serial execution and bounded failure behavior

Depth work SHALL use the existing request lock, single-thread GPU executor, local admission lease, and residency lifecycle. Failed inference SHALL retain failed-or-unknown semantics without automatic retry or substitution.

#### Scenario: Concurrent guided requests do not overlap

- **WHEN** two different guided requests arrive concurrently
- **THEN** GPU calls do not overlap, receipts retain their own guide/scale, and leases release exactly once

#### Scenario: Fleet-authenticated local admission

- **WHEN** `fleet_token_file` is configured with a principal authorized for `harmony-image`
- **THEN** the worker passes that raw token to `admit` without logging it, and lease acquisition/release proceeds; absent configuration passes no token as before

#### Scenario: Inference failure is not retried blindly

- **WHEN** guided inference raises or returns an invalid image
- **THEN** the attempt fails with retained evidence and no alternate model, quantization, or pipeline is tried

### Requirement: Isolated qualification and dispatch handoff

Real qualification SHALL use dedicated test hostd, gateway, and worker processes with no production peers. The worker SHALL use `LIVESTACK_REGISTER=0`, only the isolated broker facade, a distinct depth kind, and owner-approved device mapping. Production broker and wrapper paths SHALL NOT be used. The lifecycle SHALL provide `up --config <owner-variant.json> --output <handoff>`, `smoke --dispatch <handoff> --request <request.json>`, and `down --dispatch <handoff>` plus a credential-free `livestack.image-depth-dispatch.v1` handoff. Tracked NF4/INT8 smoke requests SHALL reference an explicit guide and SHA-256 without implicit prompt, guide, or quantization.

#### Scenario: Handoff identifies the isolated environment

- **WHEN** `up` starts the isolated processes with an owner variant config and output handoff path
- **THEN** the handoff contains isolated URLs, expected host/device/unit, model/config revisions, quantization, 50 steps, CFG 4, negative-prompt/prompt-encoding settings, Torch/Diffusers versions, runtime paths, wrapper/config/code hashes, and test PIDs without credentials

#### Scenario: Text-only baseline has no ControlNet identity or allocation

- **WHEN** an isolated config omits `depth_guide` and uses the exact `qwen_image_2512_text_nf4`/`nf4` or `qwen_image_2512_text_int8`/`int8` pair
- **THEN** the lifecycle starts without ControlNet, the handoff records `depth_guide: false`, staged worker and fleet attributes contain no depth or ControlNet claims, and Qwen generation settings remain explicit

#### Scenario: Staged interpreter and revision are preserved

- **WHEN** a staged run uses a venv symlink without Git metadata and a child process has not completed exec
- **THEN** the handoff binds the explicit code revision/source manifest, the interpreter symlink is not resolved away, facade `status: "ok"` is accepted, and process identity capture waits for exec instead of treating empty argv as a crash

#### Scenario: Smoke and shutdown are verified

- **WHEN** `smoke` and then `down` run from that handoff
- **THEN** smoke decodes and verifies the actual PNG and every evidence field, and shutdown verifies command lines/config hash before stopping only the recorded test PIDs

#### Scenario: Production route or colliding fleet is never used

- **WHEN** a qualification config names production broker `127.0.0.1:8799`, the production wrapper, a non-isolated peer, or registered variants collide on the isolated fleet
- **THEN** lifecycle validation refuses before smoke and shuts down only the recorded test processes

#### Scenario: Registration is checked before smoke

- **WHEN** the isolated worker finishes registration
- **THEN** `up` verifies the expected base snapshot/revision, depth kind, device, and absence of unexpected peers before allowing `smoke`

### Requirement: Operator documentation and rollback

Operator documentation SHALL describe immutable configuration, variant kinds, isolated lifecycle, memory accounting, and rollback. Rollback SHALL NOT affect ordinary units.

#### Scenario: Rollback preserves ordinary service

- **WHEN** depth configuration and the dedicated test unit are removed
- **THEN** no depth capability is advertised and ordinary unguided requests continue through the existing managed path
