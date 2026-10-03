# SenseNova-U1.5 through Harmony

Run `python -m livestack_node.imagegen.sensenova` with `HARMONY_IMAGE_CONFIG`
pointing to a normal image-worker configuration, plus `gguf_checkpoint` and
`vram_mode` (`low`, `balanced`, `fast`, or `full`). Use the matching official
checkpoint's config and tokenizer as `snapshot`; GGUF weights are a separate
artifact. `CUDA_VISIBLE_DEVICES` and the advertised Harmony `device_id` must
identify the same physical GPU. Placement remains the broker's decision.

The runtime uses the official `sensenova_u1` loader and layer-offload context.
It reuses the managed image worker, leases, admission, idle eviction, request
validation, image ingress and grant verification. It never starts a separate
Livestack gateway or chooses an ungranted GPU.

## Request

POST to the existing `/v1/images/generations` ingress with its bearer credential:

```json
{
  "prompt": "A cinematic mountain lake at sunrise",
  "width": 768,
  "height": 768,
  "seed": 1000,
  "harmony_requires": {
    "model": "sensenova/SenseNova-U1.5-8B-MoT"
  }
}
```

`class=imagegen` and `task=text_to_image` are added by the shared contract.
The request names the model rather than a machine or unit; quantization may be
constrained with `quantization` if the caller needs a particular artifact.
The returned `harmony` evidence identifies the actual unit/device, both grants,
base-model revision, quantization, settings, latency and GPU memory peaks.

## Dependencies and artifacts

The first CUDA deployment uses Torch 2.6.0+cu124, Diffusers 0.40.0,
Transformers 4.57.6, Tokenizers 0.22.2, huggingface-hub 0.36.0 and gguf 0.19.0.
SenseNova source is pinned to
`c35c83b58ef6e238a08b986aa58f3b73303aa42f` in
`https://github.com/OpenSenseNova/SenseNova-U1` and installed as a wheel without
overwriting the qualified CUDA Torch installation. Its reference pyproject
pins Torch 2.8; this deployment deliberately uses cu124 for the 2070 driver.
The runtime's regression test exercises the real official timestep module and
real GGUF tensors, and requires these optional inference dependencies.

Official config/tokenizer revision:
`9feeeab8a2792514d109cd34589342a2cc1d4ab2`.
Quantized weights: `realrebelai/SenseNova-U1.5-8B_GGUFs`, revision
`bc2e8f83688489e6b465daa833e9b318ea45c9d9`, file
`SenseNova-U1.5-8B-MoT-Q8_0.gguf`.

The packed checkpoint quantizes the input linear of both timestep embedders.
The upstream module casts sinusoidal inputs to `.weight.dtype`, which is byte
storage for GGUF. `restore_timestep_dtype` restores just those small linears to
BF16 using their existing quantized values. The remaining GGUF linears stay
quantized. No external model source or shared inference environment is patched.

## Memory and qualification

For layer offload, `resident_bytes` must be a resource vector including both
`vram_bytes` and `ram_bytes`. CPU weight storage and pinned buffers consume real
host memory. Do not advertise only VRAM or assume an 8B LLM-sized footprint:
this MoT has separate understanding and generation parameter streams.

Both the fleet broker and the selected host broker need Livestack's engine-unit
host-memory admission support (present in `16575876`). Updating a checkout does
not update a systemd service whose `PYTHONPATH` points to an older release.
Use a pinned release and a final ordered drop-in, preserving existing auth,
observe-only fleet mode and device reserve settings.

An optional `bf16_modules_checkpoint` may supply official BF16 weights for
`fm_modules` outside transformer layers. The adapter restores those small
modules while leaving the large attention/MLP streams quantized. Extract the
16 FM head, timestep, noise-scale and generation-embedding tensors from the
pinned official first safetensors shard using `safetensors`; record its hash
alongside the GGUF artifact. Returned evidence records whether this override
was applied. Use only weights from the same official model revision.

Q3 produced speckled outputs on both RTX 2070 and RTX 3090 during qualification;
GPU fit alone does not qualify its image quality. Q8 with the official BF16 FM modules produces coherent 768×768 images
but retains visible grain at this sub-default resolution. Do not expose an unqualified profile as usable
capacity.

Use actual generated images to qualify a profile before publishing its capacity.
Check image contents, cold and warm timings, RAM/VRAM peaks, model identity and
matching fleet/local device grants. The supplied `sensenova-prompts.json` contains
10 reproducible qualification prompts. Sub-2048 dimensions are outside the
checkpoint's default trained resolution buckets; assess quality explicitly.
