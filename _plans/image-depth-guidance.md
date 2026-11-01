# Image depth guidance

Status: implementation and qualification in progress, `openspec/changes/add-image-depth-guidance`.

## Current assumptions that are stale for guided work

- Prompt, dimensions, and seed are sufficient request evidence.
- `task: "text_to_image"` is the only image capability that needs routing.
- Forwarding an arbitrary extra JSON field is enough to establish that a worker consumed it.
- Base-model residency is the whole memory footprint.

Those assumptions are valid for unguided generation but unsafe for depth-guided generation. The change replaces them only for the new explicit `depth_guide` capability; unguided behavior remains intact.

## Ownership and identity boundaries

- The caller owns the source scene and grounded prompt. Histo owns historical-time and evidence semantics.
- The Histo qualification CLI owns the exact output-sized PNG control image and its SHA-256. The worker must verify the same hash; a source-scene hash never substitutes for it.
- `control_image.sha256` identifies model input. Base/ControlNet revisions and ControlNet config SHA-256 identify immutable model inputs. Request/output identity additionally includes prompt, dimensions, seed, scale, and actual output dimensions.
- The Livestack contract validates bounded payload mechanics. The worker owns decoding and actual pipeline use. The gateway and Histo TypeScript CLI independently verify returned guide evidence and actual pixels.
- The host broker remains the sole residency authority. A `depth_guide` attribute is a capability constraint, not permission to load directly. Depth/quantization variants use distinct unit kinds so broker aggregation cannot merge them with ordinary workers.
- Worker configuration owns model snapshot locations, expected ControlNet revision/config hash/weight hash, and declared footprint. No request may select an arbitrary local path or trigger a download.
- Real qualification uses a dedicated test hostd/gateway/worker with no production peers and a credential-free dispatch handoff. Production broker and wrapper paths are refused.
- Existing planner, lease, and result ledgers remain authoritative. The worker result adds guide/control evidence; it does not create a parallel routing ledger or claim that grants contain guide fields.

## Qualification lifecycle and memory

- Exact pairs are `qwen_image_2512_depth_nf4` with NF4 and `qwen_image_2512_depth_int8` with INT8. Crossed pairs are refused before `attach` and again at runtime construction.
- `qualify-depth.py up --config <owner-variant.json> --output <handoff>` starts a dedicated apply-mode hostd, worker, then gateway with fresh run-scoped credentials and no production peers. `smoke --dispatch <handoff> --request <request.json>` uses an explicit tracked NF4 or INT8 request and `depth-smoke-guide.png` SHA-256. `down --dispatch <handoff>` stops only identity-checked test processes.
- Startup verifies ControlNet config class/hash and weight bytes/hash. Single-device declared footprint is base `resident_bytes + weight_bytes`; split mode charges only the configured primary `resident_bytes` to the primary placement and covers secondary devices with their own leases/budgets. Runtime allocation/reservation measurements still update residency. INT8 plus BF16 ControlNet may exceed 24 GB and must not silently substitute NF4.
- `fleet_token_file`, when configured, is passed to local `admit` without being logged. Absent configuration preserves unauthenticated admission. Gateway grant checks remain kind/device only; generation settings are checked against the selected unit's advertised attributes.
- The dispatch handoff carries isolated URLs, model/config revisions, generation settings, runtime versions, wrapper/config/code hashes, and process identities. Resume fingerprints exclude timestamps, grants, PIDs, run-specific ports, and token paths.

## Test environment and rollback

Tests stage source to `/home/ubuntu/harmony-image/livestack-depth-qualification/node-py` on `xc-tower-ubuntu`, verify source hashes, and run the qualified image virtualenv with `HF_HUB_OFFLINE=1` and `PYTHONPATH=.`; ambient local Python/pytest is not used. Real hostd runs append `/home/ubuntu/livestack/node-py/.venv/lib/python3.12/site-packages` after staged code to supply `cryptography` without changing installed packages. Preserve the image venv interpreter symlink and staged `code_revision` plus `source_manifest_sha256` when `.git` is absent. Facade health accepts both `ok: true` and `status: "ok"`, and process identity capture waits for exec. No production broker, wrapper, service, or request-time model download is used. Rollback removes the depth config and distinct test unit, leaving ordinary unguided units unchanged.

## Decision record

The normative implementation contract lives in `openspec/changes/add-image-depth-guidance/design.md` and its delta specification. The initial candidate is Qwen base plus `InstantX/Qwen-Image-ControlNet-Union` in depth mode, using exact relative depth produced by a proxy renderer. RGB img2img is not part of this contract.
