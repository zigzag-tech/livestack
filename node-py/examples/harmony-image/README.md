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
