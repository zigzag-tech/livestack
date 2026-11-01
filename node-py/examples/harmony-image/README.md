# Harmony image workers

The reusable `livestack_node.imagegen` package exposes managed Diffusers workers and a requirement-routed ingress. The ingress asks the fleet planner to select a concrete unit and device, resolves its fresh peer, and forwards the original prompt. The worker obtains local admission before inference and holds a lease while warming and generating. Refused, ambiguous, mismatched, or degraded grants fail explicitly.

Deployment on September 30, 2026:

| Host | GPU | Model | Snapshot revision |
| --- | --- | --- | --- |
| xc-tower-ubuntu | RTX 3090, GPU 1 | Qwen/Qwen-Image-2512 | 25468b98e3276ca6700de15c6628e51b7de54a26 |
| zz-joe | RTX 2070, GPU 0 | Tongyi-MAI/Z-Image-Turbo | f332072aa78be7aecdf3ee76d5c247082da564a6 |

Worker configuration is `/home/ubuntu/harmony-image/worker.json`; start it with `python -m livestack_node.imagegen.worker` and `HARMONY_IMAGE_CONFIG` pointing to that file. `CUDA_VISIBLE_DEVICES` selects the physical GPU and the configured Harmony `device_id` must match it. The worker uses the normal Harmony managed-unit lifecycle, so idle weights may be evicted to make room for other work. Qwen uses INT8 weights (NF4 introduced visible artifacts in qualification); Z uses NF4. Generator weights use configurable NF4 or INT8; CPU text encoding preserves GPU memory. The Z worker uses BF16 arithmetic and FP32 VAE decoding; FP16 generation produced blank output on the tested Turing GPU.

Pinned dependencies are in `livestack_node/imagegen/requirements.txt`. Workers additionally require the existing Harmony `shared_py` extension. Snapshots must be downloaded before setting `HF_HUB_OFFLINE=1`. Host-specific configuration and systemd units are tracked in xc-setup under `config/machines/{xc-tower-ubuntu,zz-joe}`. Credentials are separate mode-0600 files and are never checked in.

The ingress runs on `http://100.64.0.18:8211`. POST JSON to `/v1/images/generations`, with `Authorization: Bearer` read from the private worker token:

```json
{"prompt":"A red apple on a wooden table, studio photograph","width":768,"height":768,"seed":1000,"harmony_requires":{"params_b>=":20}}
```

Change only the requirement to `{"params_b<=":6}` for the Z worker. The ingress adds `class=imagegen` and `task=text_to_image`; callers do not name a worker or host. Responses include base64 PNG data and `harmony` evidence containing the pinned model revision, original request, concrete fleet/local grants, actual device, settings, timing, and memory measurements.

The qualified shared comparison resolution is 768×768. The 2070 worker rejects dimensions above 768 instead of risking the observed 1024×1024 attention-memory failure. Requests validate dimensions, prompt length, seeds, credentials, and model requirements; blank images fail instead of being reported as successful generations.

Run the resumable comparison once for each model:

```sh
python examples/harmony-image/compare.py --prompts examples/harmony-image/prompts.json --out /home/ubuntu/harmony-image/comparison --token-file /home/ubuntu/harmony-image/worker.token --model qwen
python examples/harmony-image/compare.py --prompts examples/harmony-image/prompts.json --out /home/ubuntu/harmony-image/comparison --token-file /home/ubuntu/harmony-image/worker.token --model z
```

The gallery is served on the mesh at `http://100.64.0.18:8212/`. Each result has a PNG and evidence JSON with a SHA256 hash. Equal seeds are recorded for reproducibility but do not give identical noise across model architectures. Generation settings and different GPUs are part of this comparison.

## Depth-guided generation

The image worker also supports Qwen depth ControlNet as an explicitly routed capability. It is not enabled for ordinary production workers and does not use RGB img2img. Approved unit/quantization pairs are exactly `qwen_image_2512_depth_nf4` with NF4 and `qwen_image_2512_depth_int8` with INT8; crossed pairs and ordinary-kind collisions are refused before `attach`.

A guided request uses the normal `/v1/images/generations` endpoint and adds:

- `control_image`: one output-sized PNG as strict base64 plus its lowercase SHA-256 (at most 8 MiB decoded).
- `controlnet_conditioning_scale`: 0.8–1.0 inclusive.
- `harmony_requires.depth_guide: true`, exact base model/revision, ControlNet model/revision/config hash, and quantization.

The tracked examples are `depth-worker.example.json`, `depth-smoke-request-nf4.json`, `depth-smoke-request-int8.json`, and `depth-smoke-guide.png`. The ControlNet is `InstantX/Qwen-Image-ControlNet-Union` revision `b13036f066d6dee7c20513e263d3d673055e9de8`. Startup verifies its `config.json` class and SHA-256 `6d80a5e2ab3abeb773b06f917b54a72f60c35ce91091f59f79243fb0f5d3d4bd`, and its weight byte count 3536027816 and SHA-256 `d51dca0073366a675108d5b83c3b7ef941cf2214c9a1c95c23f1e9a228ddbdb0` before advertising depth. ControlNet precision is `bf16` by default or `int8` when explicitly configured, and `controlnet_quantization` is advertised and verified in guided evidence. The declared ControlNet footprint is the full source `weight_bytes` for BF16 or half that size, rounded up, for INT8; measured runtime allocation and reservation still update residency. INT8 plus BF16 ControlNet may not fit a 24 GB GPU and must not silently fall back to NF4.

The runtime shares the base transformer, text encoder, tokenizer, VAE, and scheduler with `QwenImageControlNetPipeline`. Optional `offload: {"strategy":"sequential","controlnet_gib":null}` initializes the transformer on CPU, resets the pipeline device map, and calls `enable_sequential_cpu_offload()` with `vae` and `text_encoder` excluded so transformer blocks return to CPU after execution while VAE stays on GPU and text encoding stays on CPU. Adding `"vae":"cpu"` leaves only the text encoder excluded, keeping VAE decoding on CPU and reducing GPU residency; output dimensions and quantization are unchanged. ControlNet remains GPU-resident by default because `QwenImageControlNetModel` does not support automatic maps; adding `"controlnet":"sequential"` initializes it on CPU and applies Accelerate `cpu_offload` block hooks instead, recording `controlnet: "sequential"` in the evidence. For a hard transformer budget, use `strategy: "auto"` with a positive `transformer_gib`; this uses `device_map="auto"` and `max_memory` instead of the pipeline's sequential offload and can be combined with sequential ControlNet and CPU VAE. Use `controlnet: "cpu"` to run the entire ControlNet on CPU and move only its residuals to the GPU; this avoids GPU-resident ControlNet weights and activations without per-step cache hooks. INT8 CPU-dispatched modules use `llm_int8_enable_fp32_cpu_offload=True`. The actual strategy and device map are logged and included in managed evidence. Results include actual `image.size`, `harmony.depth_guide`, and `generation_settings` (steps, CFG, negative-prompt semantics, text encoder, and max sequence length). The gateway verifies those values against the selected unit's advertised attributes. Missing or mismatched evidence is a failure, not a text-only fallback. Eviction clears all pipeline and ControlNet references. If `fleet_token_file` is configured, its raw token is passed to local `admit` without being logged; absent configuration preserves unauthenticated admission.

A depth unit that needs a whole device runs under exclusive-device leases. The worker config sets `exclusive_device` (`min_free_bytes`, `wait_timeout_s`, `ttl_seconds`) and then refuses any request without the authenticated `X-Harmony-Device-Lease` header; it never creates a nested lease. The ingress config names the units that need leases in `exclusive_units`, keyed by unit kind: `{"sdxl_depth_int8": {"broker": "http://127.0.0.1:8799", "owner": "harmony-image", "device_ids": ["xc-tower-ubuntu/a46c4c2e"], "min_free_bytes": 12000000000, "wait_timeout_s": 240, "ttl_seconds": 120}}`. The observe-only fleet admission picks the unit first; when its kind is listed, the ingress takes the leases on that dispatching host broker, which drains the device's other tenants itself (evicts idle units, waits out busy ones and residency floors, never stops a process) and holds other admissions off the device until release. The ingress heartbeats the leases through loading and generation and releases them afterwards. Units not listed (Qwen, Z, klein) never take a lease. The process-node facade in front of a depth worker admits with the lease ids as the lease owner (`device_lease_owner` in its config), as does the worker (`harmony-image`). Results include every lease/device pair in `harmony.device_leases` and the acquisition records in `harmony.device_grants`. No degraded grant or external-process termination is permitted.

`depth_guide` is a specialist-only planner attribute: a depth unit is a candidate only for a requirement that names `depth_guide` (or the unit), so generic demand such as `{"params_b>=": 20}` keeps landing on Qwen even though the FLUX.2 depth unit also has `params_b` 32.

For multiple GPUs, set ordered `device_ids`, matching comma-separated `cuda_visible_devices`, and per-device `exclusive_device.min_free_bytes`. The first device owns the worker placement and its `resident_bytes`; secondary ControlNet weight bytes are not charged to that primary. With `offload.strategy: "auto"`, `transformer_gib` may be an object such as `{"0":2,"1":4}`; keys are logical CUDA indices after `CUDA_VISIBLE_DEVICES` mapping. `controlnet_device` selects the logical GPU for sequential ControlNet. The ingress acquires every listed device's lease in order, and the broker validates all of them but places only the primary device. A secondary device needs a memory measurement on the host broker: another facade on it reports one, or `LIVESTACK_DEVICE_METERS` adds a broker-local meter. When transformer blocks span GPUs, a forward wrapper and per-block hooks move each ControlNet residual to the corresponding block output device before addition. `offload_map` and `device_peaks` record the actual placement and per-GPU allocation/reservation peaks.

### Production depth units (xc-tower-ubuntu, 2026-11-02)

Depth generation runs only as Harmony-managed units, requested only through the production ingress `http://100.64.0.18:8211/v1/images/generations`:

| Service | Facade / private worker | Unit | Devices | Lease `min_free_bytes` |
| --- | --- | --- | --- | --- |
| `harmony-image-sdxl-depth` | 8216 / 127.0.0.1:28216 | `sdxl_depth_int8` | GPU 1 (`a46c4c2e`) | 12 GB |
| `harmony-image-flux2-depth` | 8217 / 127.0.0.1:28217 | `flux_2_dev_depth_int8` | GPU 1 + GPU 0 (`a46c4c2e`, `4bac2869`) | 20 GB each |

Each service is a CPU-only process-node facade, registered with the host broker (8799) and fleetd (8801), that starts the GPU worker only when Harmony warms the unit and reaps its whole cgroup after `idle_seconds` (60 s SDXL, 30 s FLUX.2). Worker configs are `~/harmony-image/depth/{sdxl,flux2}-depth-worker.json`; facade configs are `~/.local/share/harmony-process-node/harmony-image-{sdxl,flux2}-depth.json`; both are tracked in xc-setup under `config/machines/xc-tower-ubuntu/harmony-image-depth/`. Model pins, hashes and generation settings are exactly the qualified ones below.

`qualify-depth.py` is a client of that ingress and nothing else: it attaches a guide PNG, posts the request, and verifies the PNG plus the lease, grant and depth evidence before writing the image and a receipt.

```sh
python examples/harmony-image/qualify-depth.py --request request.json --guide depth.png --out out/image.png
```

Add `"harmony_wait_s": 300` to a request that may wait for Harmony to clear the device (a unit inside its residency floor, a busy tenant). A FLUX.2 run holds both GPUs exclusively for about 25 minutes (qualified 768×432: 149 s load, 1515 s total), so other tenants on this host wait for it. Two depth requests need not be sequenced by the caller: while one holds a device's lease, the next queues for it on the host broker (FIFO per device, bounded by its `harmony_wait_s`; `wait_s` on an exclusive `/admit`) and starts when the lease is released or its holder stops heartbeating and the lease lapses at its TTL. With `harmony_wait_s: 0`, or once the window runs out, it is refused with `state: other_owner` and the reason.

Never start a private broker, worker or gateway for GPU work. The removed `up` lifecycle did that with `LIVESTACK_REGISTER=0`; production Harmony could not see its GPU memory, could not arbitrate, and polytts/polyasr/LLM tenants were refused for 15+ minutes (2026-11-01).

### SDXL depth qualification

The `sdxl_depth_int8` unit supports `stabilityai/stable-diffusion-xl-base-1.0` with `diffusers/controlnet-depth-sdxl-1.0`. The UNet is INT8, the depth ControlNet is BF16, both text encoders are CPU FP32, and VAE decoding is GPU FP32. Its worker configuration uses `guidance_scale` instead of Qwen's `true_cfg_scale` and permits 20–50 inference steps. The same guided request contract, full revision/config/weight pinning, exclusive-device lifecycle, and evidence verification apply. This runtime does not accept Qwen transformer-offload settings.

### FLUX.2 Fun Union depth qualification (experimental)

The `flux_2_dev_depth_int8` unit loads `black-forest-labs/FLUX.2-dev` with the integrated `alibaba-pai/FLUX.2-dev-Fun-Controlnet-Union` 2602 controller through the pinned VideoX-Fun pipeline. This is not a stock Diffusers ControlNet: the controller adds blocks to the 32B transformer. The transformer loads as INT8 through a meta-device, shard-streaming adapter and is dispatched across both logical GPUs using the configured `transformer_gib` budgets. Text encoding is CPU INT8 and VAE work is GPU BF16. `videox_fun_root` is mandatory. The controller's class/config/weight hashes, actual device map, per-device peaks, guidance, and control interval are included in evidence. Do not substitute FP8 or NF4 base checkpoints.

## FLUX.2-klein on zz-joe (2026-10-02)

`harmony-klein-0` / `-1` (GPU 0 / 1, RTX 2070 8 GB, ports 8213 / 8214) run
`python -m livestack_node.imagegen.klein` from release
`~/.local/share/livestack-releases/learned-footprint-f15cda20` (drop-in
`60-learned-footprint.conf`, which also gives each process its own learned-footprint
store, `~/.cache/livestack/activation-zz-joe-klein-N.json`). The NF4 text encoder
stays on the GPU for the whole residency; host RAM holds no weights (cgroup anon
1.36 GB after a generation). The old `~/harmony-image/klein/klein_worker.py` is no
longer run.

`resident_bytes` in `~/harmony-image/klein/worker-N.json` is the declared prior
(3e9); the node replaces it with what it measures (openspec `learned-gpu-footprint`).
The 5e9 hand-set on 2026-10-02 02:52 UTC was removed. Measured on klein-0, first load
and one 768 px generation after the deploy (seed 7):

| | before (hand-set) | after (learned) |
|---|---|---|
| `footprint` | 5.00e9, `declared` | 4.876e9 (allocator reserved growth across the load), `allocator` |
| `activation_headroom` | none (store discarded by the 5e9 edit) | 2.338e9 (peak reserved over the op baseline) |
| planned peak | — | 7.214e9 = the generation's `peak_reserved_bytes` |

nvidia-smi showed 4974 MiB on GPU 0 while resident (the unit plus the process's CUDA
context, which stays when the unit is evicted and is therefore not charged to it).
Timing: load 16.9 s, prompt encoding 1.6 s, generation 19.7 s.

klein-1 reports the declared prior with `learned.state: "unmeasured"` until Harmony
first places the unit on GPU 1: while klein-0 holds it resident, admission grants that
copy, and a request sent straight to 8214 is refused ("no concrete local Harmony
grant"), as designed.
