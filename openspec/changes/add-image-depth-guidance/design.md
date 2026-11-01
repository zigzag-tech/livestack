# Design: image depth guidance

## Context

The current request validates prompt/dimensions/seed but preserves extra fields; the runtime is text-only, and the gateway does not prove guide consumption. In addition, broker aggregation folds peers of one `unit` kind into one planner unit, so adding depth attributes to an existing Qwen kind can hide capability, misroute a grant, or charge ordinary workers the ControlNet footprint.

The candidate is Qwen base plus `InstantX/Qwen-Image-ControlNet-Union` in depth mode. This change implements the mechanical managed capability. Histo owns historical and visual qualification.

## Goals / Non-goals

**Goals**

- Explicit, bounded depth requests and fail-closed capability routing.
- Immutable base/ControlNet/config/guide identity and actual-output evidence.
- One managed unit with shared base components and correct release behavior.
- Real qualification on a dedicated broker/gateway with no production peers.

**Non-goals**

- RGB img2img, other control modes, historical interpretation, or visual-quality admission.
- Production activation, request-time downloads, or caller-selected model paths.
- Changing existing planner/hostd ledgers or ordinary unit kinds.

## Decisions

### 1. Request JSON contract

Keep `task: "text_to_image"`; depth is an additional capability. Both guided and unguided requests require `req.get("task", "text_to_image") == "text_to_image"` and `class: "imagegen"`; a supplied wrong task is invalid.

A guided request is:

```json
{
  "prompt": "Grounded visual description",
  "width": 768,
  "height": 432,
  "seed": 1863,
  "control_image": {
    "media_type": "image/png",
    "b64_json": "<base64 PNG bytes>",
    "sha256": "<64 lowercase hex characters>"
  },
  "controlnet_conditioning_scale": 0.8,
  "harmony_requires": {
    "class": "imagegen",
    "task": "text_to_image",
    "depth_guide": true,
    "model": "Qwen/Qwen-Image-2512",
    "revision": "25468b98e3276ca6700de15c6628e51b7de54a26",
    "controlnet_model": "InstantX/Qwen-Image-ControlNet-Union",
    "controlnet_revision": "b13036f066d6dee7c20513e263d3d673055e9de8",
    "controlnet_config_sha256": "6d80a5e2ab3abeb773b06f917b54a72f60c35ce91091f59f79243fb0f5d3d4bd",
    "quantization": "nf4"
  }
}
```

Validation in `contract.py`:

- Preserve the JSON-shaped `control_image`; decoded bytes/PIL exist only transiently and are never inserted into the forwarded request.
- Require strict base64, at most 8 MiB decoded bytes, exact lowercase SHA-256, one RGB-readable PNG, and exact requested output dimensions.
- Require a non-boolean finite numeric scale from 0.8 through 1.0 inclusive. Require literal JSON `true` for `depth_guide`; numeric 1, booleans used as scale, and wrong task/class are invalid.
- Require image and `depth_guide: true` together, and scale exactly when an image is present. Guided requirements also name exact base model/revision, ControlNet model/revision/config hash, and quantization.
- Existing prompt, seed, and dimension checks remain. Other metadata never activates guidance.

The qualification path requires the pinned base `revision` for text baselines as well as guided requests. Existing callers that do not require a base revision retain current behavior.

### 2. Unit kinds and attributes

Workers with a configured base `revision` advertise it; qualification configurations require it, while legacy unguided fixtures without that field remain valid. A depth worker additionally advertises `depth_guide: true`, `controlnet_model`, `controlnet_revision`, `controlnet_config_sha256`, `steps`, `true_cfg_scale`, negative-prompt semantics, `text_encoder`, and `max_sequence_length`; unconfigured workers omit all depth attributes. Advertised generation settings are the gateway's authoritative expected values and are required only for depth workers.

**A depth/quantization/footprint variant MUST use a distinct `unit` kind from every coexisting variant.** This change permits exactly two pairs: `qwen_image_2512_depth_nf4` **with `nf4` only**, and `qwen_image_2512_depth_int8` **with `int8` only**. Ordinary `qwen_image_2512` is unchanged. The exact-pair rule prevents mixed-variant aggregation by construction rather than requiring a startup broker query.

Tests cover two peers with mixed attributes and both crossed pairs (`nf4` kind + INT8 config and INT8 kind + NF4 config). Guided requests cannot fall back to an unconfigured worker. Unguided requests can use a depth worker in an isolated qualification fleet, with no implication that it should replace ordinary production workers.

### 3. Immutable startup verification

`worker.py` accepts optional `depth_guide` config:

```json
{
  "model": "InstantX/Qwen-Image-ControlNet-Union",
  "revision": "b13036f066d6dee7c20513e263d3d673055e9de8",
  "snapshot": "/owner-configured/local/snapshot/b13036f066d6dee7c20513e263d3d673055e9de8",
  "config_sha256": "6d80a5e2ab3abeb773b06f917b54a72f60c35ce91091f59f79243fb0f5d3d4bd",
  "weight_sha256": "d51dca0073366a675108d5b83c3b7ef941cf2214c9a1c95c23f1e9a228ddbdb0",
  "weight_bytes": 3536027816
}
```

Before `attach` or capability advertisement:

- Require base `config["model"] == "Qwen/Qwen-Image-2512"`, a full base revision, base snapshot basename equal to that revision, and the exact `(unit, quantization)` pair `("qwen_image_2512_depth_nf4", "nf4")` or `("qwen_image_2512_depth_int8", "int8")`. Runtime construction repeats both checks defensively before model loading.
- Require ControlNet snapshot basename equal to ControlNet revision.
- Parse `snapshot/config.json`, require `_class_name == "QwenImageControlNetModel"`, and verify its bytes against `config_sha256`.
- Verify `snapshot/diffusion_pytorch_model.safetensors` against configured `weight_bytes` and `weight_sha256`. Production example/qualification uses the pinned values above; temporary tests verify against their own configured values.
- Perform no model-hub access or request-time download. Missing/malformed/changed config or weights, non-Qwen base, base snapshot/revision mismatch, unsupported/crossed quantization and kind, ControlNet revision mismatch, or a non-allowlisted kind aborts startup with a typed error.

The declared initial footprint is `resident_bytes + weight_bytes` for BF16 ControlNet, or `resident_bytes + ceil(weight_bytes / 2)` for INT8 ControlNet; the source hashes remain identical and measured runtime allocation/reservation continues through existing learning. Startup emits bounded model/revision/config/weight verification or refusal records.

### 4. Managed construction, generation, and release

With depth enabled, `ImageRuntime` loads `QwenImageControlNetModel.from_pretrained(snapshot, torch_dtype=torch.bfloat16, local_files_only=True)` on the managed device and directly constructs:

```python
QwenImageControlNetPipeline(
    scheduler=text_pipe.scheduler,
    vae=text_pipe.vae,
    text_encoder=text_pipe.text_encoder,
    tokenizer=text_pipe.tokenizer,
    transformer=text_pipe.transformer,
    controlnet=controlnet,
)
```

This shares all base components. It does not create an img2img pipeline or second base model. Existing CPU FP32 prompt encoding, VAE tiling/slicing, serial executor, residency, and lease behavior remain.

Guided generation passes the decoded guide, exact requested `width`/`height`, scale, control interval 0.0–1.0, seed, configured steps, and existing Qwen prompt/negative settings. Add an actual `image.size == (width, height)` runtime guard in addition to the blank-image guard; model-default 1024×1024 output fails.

Unguided generation uses the existing text pipeline. Guided input on an unconfigured runtime raises instead of falling back. `close()` clears every text/depth pipeline and standalone ControlNet reference before `gc.collect()` and `empty_cache()`; close/free/reload tests prove references are gone.

### 5. Evidence and actual dimensions

`ImageRuntime.generate` returns `depth_guide` metrics for guided requests and `None` otherwise:

```json
{
  "image_sha256": "<decoded PNG SHA-256>",
  "conditioning_scale": 0.8,
  "controlnet_model": "InstantX/Qwen-Image-ControlNet-Union",
  "controlnet_revision": "b13036f066d6dee7c20513e263d3d673055e9de8",
  "controlnet_config_sha256": "6d80a5e2ab3abeb773b06f917b54a72f60c35ce91091f59f79243fb0f5d3d4bd",
  "control_guidance_start": 0.0,
  "control_guidance_end": 1.0
}
```

The worker writes `harmony.width` and `harmony.height` from actual `image.size`, not the request. It also returns `generation_settings` with `steps`, `true_cfg_scale`, negative-prompt semantics, `text_encoder`, and `max_sequence_length`. The gateway compares guided evidence field-for-field with the validated request and compares generation settings against `target["unit"]["attributes"]`, the selected depth worker's advertised authoritative values. Fleet-grant verification remains limited to `kind` and `device_id`; a grant is not claimed to contain guide fields.

Existing admission requests/snapshots and worker results retain requirement/consumption identity. Unguided results report `depth_guide: null`, but old unguided gateway test doubles are not forced to add that field.

### 6. Dedicated qualification lifecycle

`node-py/examples/harmony-image/qualify-depth.py` implements `up`, `smoke`, and `down`:

- `up --config <owner-variant.json> --output <handoff>` starts a dedicated test `hostd`, image gateway, and worker with **no production peers**. Set `LIVESTACK_REGISTER=0` on the test worker, seed only the isolated broker's facade, and point worker `broker` and gateway `fleet` only to that broker. Never use production broker `127.0.0.1:8799`.
- After registration and before `smoke`, `up` reads the isolated `/fleet`, enforces the expected base snapshot/revision and exact unit/quantization pair, and refuses/shuts down on any crossed pair, variant-kind collision, or unexpected peer. This is a lifecycle check, not a startup broker query.
- Use a distinct depth unit kind and owner-approved `CUDA_VISIBLE_DEVICES`; logical `cuda:0` must equal the granted physical device. `HF_HUB_OFFLINE=1` prevents model-hub access while managed HTTP continues to work.
- `up` writes a credential-free `livestack.image-depth-dispatch.v1` handoff with isolated broker/gateway URLs, owner host, expected host/device/unit, base/ControlNet/config revisions, quantization, 50 steps, CFG 4, the configured negative prompt (empty or the pinned Qwen 2512 quality negative prompt), CPU FP32 text encoding/max 512 tokens, Torch/Diffusers versions, Python/PYTHONPATH, token-file path (not contents), staged wrapper path/hash, worker config hash, Livestack code revision, and test PIDs.
- Stage the owner wrapper under a unique qualification path and verify its hash. Never overwrite `/home/ubuntu/harmony-image/generate-creative-image.py`.
- `smoke --dispatch <handoff> --request <request.json>` sends the explicit request, decodes the PNG, verifies actual dimensions and every evidence field, and writes `smoke.json`. Tracked `depth-smoke-request-nf4.json` and `depth-smoke-request-int8.json` reference `depth-smoke-guide.png` and its SHA-256; there is no implicit prompt, guide, or quantization. `down --dispatch <handoff>` verifies PID command lines/config hash and stops only those test processes.

For staged sources without Git metadata, `code_revision` is required and `source_manifest_sha256` is retained in the environment fingerprint. The interpreter path uses `os.path.abspath`, preserving a venv symlink instead of resolving it to the system Python. Facade health accepts both `ok: true` and `status: "ok"`. Process identity capture waits up to five seconds for exec before comparing argv, avoiding fork/exec races.

The handoff contains no credentials, tokens, or production endpoints. A same-configuration restart receives a new handoff/PIDs but the same environment fingerprint; timestamps, grant IDs, and PIDs are excluded from resume identity.

### 7. Failure and concurrency

- Contract errors return 400; unsatisfied capability returns 422 or an explicit planner refusal. JSON serialization is tested through the gateway.
- Existing request lock, busy counter, single GPU executor, lease, and residency lifecycle cover guided work. Concurrent tests assert no overlap/crossed receipts and exactly-once lease release.
- When `fleet_token_file` is configured, the worker reads that raw token and passes it to `admit` without logging it; its principal must authorize `owner_id: "harmony-image"`. Absent configuration preserves the existing unauthenticated admission call.
- Inference failure is failed-or-unknown with retained evidence and no automatic retry, model/quantization substitution, or direct fallback.
- Rollback removes depth configuration and the distinct test unit; ordinary units remain unchanged.

## Risks / Trade-offs

- **INT8 plus BF16 ControlNet may exceed 24 GB** → Use explicitly configured INT8 ControlNet and bounded CPU offload with FP32 CPU-dispatched modules, or retain a typed blocker; never silently use NF4.
- **Depth is soft guidance, not calibrated camera recovery** → Histo must qualify layout separately.
- **Config and weight hashing adds startup I/O** → Verify once per process, not per request or warm load.
- **Dedicated lifecycle is more complex than a production-worker shortcut** → It is required to prevent qualification traffic from serving or degrading ordinary requests.

## Open questions deferred to Histo

Prompt wording, historical policy, multi-camera quality, INT8 visual quality, and storyboard admission are outside this mechanical contract.
