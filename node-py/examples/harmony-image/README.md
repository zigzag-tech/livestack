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
