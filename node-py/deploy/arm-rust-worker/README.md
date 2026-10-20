# ARM Rust worker review candidate

Status: this directory contains a historical review candidate, not the current
live configuration. The candidate itself remains non-runnable and is not an
installation manifest. The ARM service and handler were later installed by the
operator; the latest owner and read-only state is recorded in
`../../../openspec/changes/reuse-task-environments/evidence/arm-worker-enrollment-owner-readback-20261017.md`
and the current loaded unit/release input hashes are in
`../../../openspec/changes/reuse-task-environments/evidence/arm-worker-current-input-readback-20261018.md`.
The service is active while job claiming remains disabled. Authority policy,
caller grants, immutable unit manifest/min-authority readback, and same-handle
ABI acceptance are still gated.

`worker-config.review.json` records known values and leaves unverified paths,
credentials, quota IDs, host capacity, verifier/capture inputs, and the unit
manifest as `null`. It contains no secret or authority grant. Validate the
review record with its adjacent Draft 2020-12 schema:

```sh
python3 -c 'import json, jsonschema; from pathlib import Path; d=Path("node-py/deploy/arm-rust-worker"); s=json.loads((d/"worker-config.review.schema.json").read_text()); c=json.loads((d/"worker-config.review.json").read_text()); jsonschema.Draft202012Validator.check_schema(s); jsonschema.Draft202012Validator(s).validate(c); print("review record valid; deployable=false")'
```

This schema validates the review record, not the final private `worker.json`.
The current worker service reads its JSON directly and has no whole-file runtime
schema. Do not pass this review record to `worker_service`; it is deliberately
not a runnable config. The owner must fill verified values into a separate
mode-0600 runtime config, validate that config under the agreed runtime schema,
and keep its token out of this worktree and all handoffs.

## Immutable code candidates

- Handler ID: `benchday.compilation.rust-check.aarch64.v1`.
- Handler package: `/tmp/taskenv-arm-rust-handler-candidate-0acf9314`, release
  digest `a0f96feed8d7c4fb0febccdbc2dc9dd1eb0abc0f9887191fa34c18ab2ab7624d`,
  archive SHA-256 `18faf52e39430338e5aa6026446d876f7cd7118d449270ed947a4579c6864199`.
- Worker service code: `/tmp/taskenv-arm-worker-service-candidate-cfdc8d30-20261013`,
  source commit `cfdc8d30d0eb663ebd86f287701417cf880070c4`, content SHA-256
  `5e795a4f7ec6ed608543437014a4bd2bb9c3764c0932d1e5d33cf0649450623c`, 262
  regular files. The target-side runbook path in the unit is the standard
  versioned release location; the candidate has not been staged there.

Before any operator installation, verify the target copy against this exact
release with `node-py/scripts/build-worker-release.py verify`. The new worker
must remain an alias of physical host `xc-mac-studio`, claim only the ARM Rust
handler and `rust` class, and add no host capacity. Keep the existing broad Mac
worker untouched and automatic task-environment selection disabled.

## Deliberately unresolved inputs

- Authority URL, worker credential, worker state/workspace paths, handler
  registry path, task-environment mount/helper/project-ID range, and shared
  worker capacity all remain `null`. Do not guess them.
- The task-environment profile name is known (`benchday-linux-rust-dev-v1`),
  but its exact ARM probe and cache-component config remains `null` pending the
  current profile/readback. The guest Rust tools are in
  `/home/ubuntu.guest/.cargo/bin`, which is not on its login `PATH`.
- The fresh x86 toolchain on `zz-joe` is Rust/Cargo 1.98.1; the ARM guest has
  Rust/Cargo 1.99.0. Their commit/date and LLVM identities differ, and x86
  rustup has no 1.99.0 toolchain. Do not run the same-handle ABI sequence until
  the owner selects and verifies one matching toolchain on both targets.
- The owner-directed `min_authority` metadata value is `c2930611`, tied to the
  verified active authority release. The current unit code does not compare
  this field; `min_authority_runtime_enforced` is false. Do not use stale
  `c295e76a` metadata or describe this as runtime-enforced. If runtime
  enforcement is later required, implement and test that behavior separately.
- The live controller readback is recorded in `worker-config.review.json`:
  at 2026-10-14 00:10 UTC, rollout mode was observe, spec generation was 8,
  and the reconciler had applied no actions. The existing broad Mac Harmony
  worker was registered, connected, idle, and eligible, but had no ARM Rust
  handler and its rollout unit was undeclared. Verifier digest, measured
  capture size/cap, candidate unit manifest ID/digest, and a fresh target
  idle/cleanup readback remain `null`. Keep the rollout controller
  observe-only. This unit does not install or roll back anything.

Storage values reflect the approved plan: an 8 GiB workspace plus a separate
16 GiB ext4 `prjquota` environment volume, with a 15 GiB aggregate/per-replica
limit and 1 GiB reserve. The existing root filesystem lacks `prjquota` and is
not the environment volume. The 2026-10-14 00:10 UTC guest readback showed a
102 GB root disk (`/dev/vda1`, ext4, no `prjquota`) and only the small Lima
metadata ISO as a second device; the separate 16 GiB volume was not attached.
Quota helper installation, project-ID allocation, EDQUOT positive control,
and live cleanup remain operator gates.

## Additive guest provisioning plan (prepared, not run)

The live host reports Lima 2.2.0 and the instance config is
`/Users/ubuntu/.lima-harmony/xc-mac-studio-harmony/lima.yaml`. Lima's installed
default template documents `additionalDisks` with `format` and `fsType`, and
mounts the disk in the guest as `/mnt/lima-${name}`. Use the short disk name
`taskenv`; an upstream Lima issue documented silent reformatting for long
ext4 disk names, and a short name avoids that label-length hazard.

After the owner obtains a fresh target idle/cleanup guard, the host-side disk
creation command is:

```sh
LIMA_HOME="$HOME/.lima-harmony" /opt/homebrew/bin/limactl disk create taskenv --size 16GiB
```

Add this entry to the existing instance YAML without replacing its other
settings:

```yaml
additionalDisks:
  - name: taskenv
    format: true
    fsType: ext4
```

Attaching the device requires a planned instance start cycle. The owner should
use the supported Mac guest maintenance procedure after another fresh
idle/cleanup readback; this plan did not create a disk, edit the Lima YAML, or
restart the VM.

Before enrolling the worker, verify that `/mnt/lima-taskenv` is a direct ext4
block-device mount with `prjquota` enabled and that this persists across the
next planned start. The installed Lima template documents no mount-option field
for `additionalDisks`; if the automatic mount lacks `prjquota`, stop and have
the owner set up and verify a persistent guest mount before proceeding. The
environment store requires its configured root to be the separate mountpoint
itself, root-owned, in the worker group, and mode `01770`.

Use the existing helper implementation at
`node-py/livestack_node/workloads/environment_quota.py`. Its root-owned config
is `/etc/livestack/workload-environment-quota.json`; it must name the verified
mountpoint, set the maximum to 15 GiB, and use a project-ID range allocated by
the owner. Install the helper at an owner-approved absolute path and set that
path in the separate worker config. Keep the runtime config root-owned and
mode `0600`; the review JSON in this directory is not runnable. Run the helper
`probe` against the real mount, then verify a disposable environment reaches
`EDQUOT` at its limit and a second project remains writable. Keep the 8 GiB
workspace separate and reserve 1 GiB on the environment volume.

Only after those checks should the owner enroll
`xc-mac-studio-harmony-rust` as an alias of physical host `xc-mac-studio`, with
only the ARM Rust-check handler and Rust compilation class. Keep the existing
broad Mac worker untouched, add no host capacity, leave the new identity
claim-disabled until reviewed, and keep controller mode observe-only and
automatic selection disabled. Read back the installed handler digest,
capability, canary result, cleanup, and rollback path before considering the
unit complete. On rollback, drain/disable the new identity through the normal
worker controls and retain the disk until all parked environment data has been
verified clean.
