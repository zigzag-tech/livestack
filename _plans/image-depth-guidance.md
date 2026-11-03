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
- Superseded 2026-11-02: depth generation runs only as Harmony-managed units registered with the production host broker and requested through the production ingress (see `node-py/examples/harmony-image/README.md`, "Production depth units"). The dedicated test hostd/gateway/worker lifecycle was removed: production Harmony could not see its GPU memory and polytts/polyasr/LLM tenants were refused for 15+ minutes.
- Existing planner, lease, and result ledgers remain authoritative. The worker result adds guide/control evidence; it does not create a parallel routing ledger or claim that grants contain guide fields.

## Qualification lifecycle and memory

- Exact pairs are `qwen_image_2512_depth_nf4` with NF4 and `qwen_image_2512_depth_int8` with INT8. Crossed pairs are refused before `attach` and again at runtime construction.
- `qualify-depth.py --request <request.json> --guide <depth.png> --out <image.png>` posts one guided request to the production ingress and verifies the PNG, lease, grant and depth evidence. It has no lifecycle; the `up`/`smoke`/`down` commands are gone.
- Startup verifies ControlNet config class/hash and weight bytes/hash. Single-device declared footprint is base `resident_bytes + weight_bytes`; split mode charges only the configured primary `resident_bytes` to the primary placement and covers secondary devices with their own leases/budgets. Runtime allocation/reservation measurements still update residency. INT8 plus BF16 ControlNet may exceed 24 GB and must not silently substitute NF4.
- `fleet_token_file`, when configured, is passed to local `admit` without being logged. Absent configuration preserves unauthenticated admission. Gateway grant checks remain kind/device only; generation settings are checked against the selected unit's advertised attributes.

## Test environment and rollback

Tests stage source to `/home/ubuntu/harmony-image/livestack-depth-qualification/node-py` on `xc-tower-ubuntu`, verify source hashes, and run the qualified image virtualenv with `HF_HUB_OFFLINE=1` and `PYTHONPATH=.`; ambient local Python/pytest is not used. Real hostd runs append `/home/ubuntu/livestack/node-py/.venv/lib/python3.12/site-packages` after staged code to supply `cryptography` without changing installed packages. Preserve the image venv interpreter symlink and staged `code_revision` plus `source_manifest_sha256` when `.git` is absent. Facade health accepts both `ok: true` and `status: "ok"`, and process identity capture waits for exec. No production broker, wrapper, service, or request-time model download is used. Rollback removes the depth config and distinct test unit, leaving ordinary unguided units unchanged.

## Decision record

The normative implementation contract lives in `openspec/changes/add-image-depth-guidance/design.md` and its delta specification. The initial candidate is Qwen base plus `InstantX/Qwen-Image-ControlNet-Union` in depth mode, using exact relative depth produced by a proxy renderer. RGB img2img is not part of this contract.
