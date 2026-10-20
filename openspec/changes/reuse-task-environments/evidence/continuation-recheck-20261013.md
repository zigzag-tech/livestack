# Continuation recheck — 2026-10-13

This recheck followed the supervised native resume. `benchday whoami --json`
resolved a daemon-attested agent on `lappy-bellinzona`; scoped claims for the
ARM Rust caller/handler paths and the Livestack worker acceptance paths were
accepted. No pane binding was created manually.

## Worker completion handoff

The full worker suite had reported a late `lease lost` warning when a heartbeat
received HTTP 409 after `worker/complete` had already ended the attempt. The
worker now marks completion handoff before sending `worker/complete`. A 4xx
heartbeat refusal after that point stops renewal without changing the lost
lease flag; the completion response remains authoritative. A refusal before
handoff still loses the lease.

Validation on Python 3.13.15:

- The regression, pre-handoff refusal control, and short-outage worker test:
  3 passed in 7.97 s.
- `tests/test_workload_lease.py`: 13 passed in 6.83 s.
- The longer-than-lease outage/fencing control: 1 passed in 5.01 s.
- Full `tests/test_workload_worker.py`: 77 passed, 6 skipped, 1 failed in
  203.00 s. The only failure remains
  `test_receipt_resources_move_by_the_known_amount_and_say_where_they_came_from`:
  `disk_delta_bytes` was 0 after its 24 MiB write. `/home` is Btrfs, while this
  metric reads filesystem-wide `statvfs` usage. The assertion is unchanged.
  The earlier bounded-ext4 pass is recorded in
  `owner-landed-upload-rerun-20261011.md`; this full run was not on that ext4
  volume.

The lease correction landed on Livestack `main` as
`cfdc8d30d0eb663ebd86f287701417cf880070c4`. The source-only commit contains
`lease.py`, `worker.py`, and `test_workload_lease.py`; the existing OpenSpec
design/spec/task edits and candidate artifacts were not swept into it. The
task worktree now matches `origin/main` for source code.

## ARM caller and bundle checks

- `scripts/test_submit_rust_check.py`: 19 passed when run through
  `executeWithPinnedRuntime()`. A direct Python invocation bypassed runtime
  materialization and failed during fixture setup while opening `schema.sql`;
  that was invalid setup, not a product failure.
- `scripts/test_task_environment_handler_release.py`: 6 passed.
- `node --test scripts/build-handler-bundle.test.mjs`: 8 passed, 3 admitted-only
  skips; ARM and Apple bundle cases passed.
- `scripts/rust-remote.test.sh`: unmanaged execution refused before source
  capture or compiler launch.
- `openspec validate --specs`: Benchday 208 passed, Livestack 12 passed, no
  failures.

The task-environment default config is absent at
`~/.config/benchday/task-environments.json`, so automatic selection remains
disabled. No default changed, and no live worker/service, quota, or rollout
controller changed.

The separate ARM review record validates against its Draft 2020-12 schema and
states `deployable=false`; `systemd-analyze verify` accepts the unit template.
The refreshed worker service candidate is
`/tmp/taskenv-arm-worker-service-candidate-cfdc8d30-20261013`, source commit
`cfdc8d30d0eb663ebd86f287701417cf880070c4`, content SHA-256
`5e795a4f7ec6ed608543437014a4bd2bb9c3764c0932d1e5d33cf0649450623c`, 262
regular files; the repeat `hash` read returned the same digest. After the fresh
controller readback and owner direction for `min_authority`, the SHA-256
values for the review JSON, schema, and unit template at that check were
`6fca6e6026f00d0807660810df6633cb8d7f10a07a5ca3f8c6cfc0786ed6f1e3`,
`023f3926eb43b0a80db93d4a3a81200ab739394004db979fdb0daac05612a0f1`, and
`743bab7739ba40bc7ac5524ddcd18baad4a3164eb08409d4b618e16453fec4c3`. The
review schema requires the owner-directed `min_authority` value `c2930611` and
keeps `min_authority_runtime_enforced=false`; the record remains
`deployable=false`.
These validate only the review artifacts. Runtime credentials, quota IDs,
worker paths, verifier/capture inputs, and the immutable unit manifest remain
unverified; nothing was installed.

## Train status

ZZOPS returned five queued cargos for other assertion fingerprints and zero
stranded cargos. This change has no valid queued cargo to resubmit: its code
merge did not alter an isolated-E2E assertion definition, and the docs-only
single-commit request was rejected as incompatible. No train was duplicated or
resubmitted.

The x86/ARM toolchain identities still differ, so the required same-handle
x86_64 → aarch64 → aarch64 → x86_64 sequence remains unrun. Worker enrollment,
the live canary, cleanup/rollback readback, and automatic-selection activation
remain owner gates.

## Owner-fix worker-suite rerun — 2026-10-13

The Livestack task worktree remained at `cfdc8d30d0eb663ebd86f287701417cf880070c4`,
equal to `origin/main`. It contains owner-landed upload handling fix
`000e253fcce1c1e2a6f4c252e944d77675496bd9` and retry-test assertion fix
`94128fcf17d58d0ed591a7887ee06b4c3f26328b`; neither
`node-py/livestack_node/workloads/transfer.py` nor
`node-py/tests/test_workload_worker.py` has a local diff.

The complete worker test file was rerun with a fresh home-based pytest temp:

```text
node-py/.venv/bin/python -m pytest -q node-py/tests/test_workload_worker.py \
  --basetemp=/home/ubuntu/.cache/pytest-livestack-upload-retry.COljJf
```

Result: **78 passed, 6 skipped in 211.91 seconds**, exit 0. The earlier Btrfs
disk-delta failure did not recur in this run. This is local worker-suite
acceptance; no shared worker, service, quota, or policy was changed.

## Fresh live worker and rollout readback — 2026-10-13 23:54 UTC

Read-only `workers`, `get`, `environment get`, and `rollout status` calls
confirmed the remaining runtime gates:

- The successful x86 job `bb324151d575451c8952d0cdc8bb002c` ran on
  `zz-joe-e2e-4`. Its result names Rust handler release digest
  `27f65e8015d4685ca82a8931163f6a6d22042160834d5283c87a512707c77ae2`.
  The separate review candidate release digest is
  `eda3721c52ae1a78cdd42498d18dc223b6b864b3c6ba7d0887e99e819e720c9c`, so the
  candidate is not what this job ran.
- Downloaded `rust-check.json` matched artifact SHA-256
  `3c6c0ddf4794c8fbfe0b470e16b456f5870a6fa75abc2dca271fdcc05737c9a1`. It
  contains the toolchain digest and target `cli`, but rustc, Cargo, and host
  fields are absent. The parked handle remains generation 50 with
  1,412,456,448 bytes retained; no attempt is holding its CPU/RAM.
- Worker `xc-mac-studio-harmony` is connected, idle, and eligible on physical
  host `xc-mac-studio`, and reports Linux/aarch64. Its handlers do not include
  `benchday.compilation.rust-check.aarch64.v1`; its rollout unit is
  `undeclared`, and the rollout spec has no Mac worker entry or set.
- The fresh rollout report is generation 14419, spec generation 8, mode
  `observe`; all proposed actions have `observed_only: true`. The configured
  `zz-joe-e2e` group remains behind/unknown. No rollout action was applied.
- Two currently queued `benchday.e2e.task.v1` jobs,
  `b09a8a82a8404b31942138e2dda9c883` and
  `72669c4ad8ca46f696e04201b2aab7cc`, belong to `benchday-owner` and select
  `glance.hub-down-client-merge` at commit
  `88536bfd5962fbeb4a92dfef5bd8713ec20890b3`. They are unrelated to this
  change and were left untouched.

The rollout's existing unit manifests still contain `min_authority: c295e76a`;
that field is metadata only in the current unit code and is not evidence of
runtime enforcement. The review candidate and its schema now set
`min_authority` to `c2930611` per the owner's instruction, while explicitly
recording that runtime enforcement is false. Its config remains
`deployable=false`. No worker, unit, quota, controller, or automatic-selection
setting was changed.

## Fresh Mac guest storage and toolchain probe — 2026-10-13 23:57 UTC

Using the non-default Lima home and `limactl shell --workdir /`, a read-only
guest check confirmed `aarch64`. Rust tools are installed under
`/home/ubuntu.guest/.cargo/bin`, which is absent from the guest login `PATH`:

- rustc 1.99.0, commit `b940084d7eb6a299eb4bfeb8e34901bc051e7ac4`, date
  2026-09-28, host `aarch64-unknown-linux-gnu`, LLVM 23.1.1;
- Cargo 1.99.0, commit `5f94df4789f005f9a352888e8355ffc645b7ed0e`, date
  2026-08-27, host `aarch64-unknown-linux-gnu`;
- installed target: `aarch64-unknown-linux-gnu`.

`findmnt` reports `/dev/vda1` as ext4 without `prjquota`. `lsblk` shows the
95 GiB root disk, EFI/boot partitions, and the Lima metadata ISO; there is no
separate 16 GiB task-environment volume. These versions differ from the
read-only x86 host shell report (Rust/Cargo 1.98.1), and the admitted x86 job
still does not provide a versioned worker receipt. No toolchain, disk, VM,
worker, or service was modified.

## Post-midnight live recheck — 2026-10-14 00:10 UTC

Read-only Livestack worker roster and rollout status still show the ARM path
unenrolled. Existing worker `xc-mac-studio-harmony` is on physical host
`xc-mac-studio`, registered and connected, idle and eligible, with no running
job. Its registered handlers do not include
`benchday.compilation.rust-check.aarch64.v1`; its unit state is `undeclared`.
Rollout remains `observe`, spec generation 8, with `applied=[]`; the rollout
spec has no worker entry for this Mac. The existing broad worker identity was
not changed.

The current seven rollout manifests still carry `min_authority: c295e76a`,
`verifier: null`, and capture size 0 with a 2 GiB cap. These fields do not
prove runtime authority enforcement or a measured capture. The separate review
candidate records `min_authority: c2930611` as metadata and explicitly says
runtime enforcement is false; its verifier, capture measurements, and unit
manifest remain unset.

`lsblk` on the guest shows a 102 GB root disk with `/dev/vda1` mounted as ext4,
EFI/boot partitions, and only the small Lima metadata ISO as a second device.
`findmnt` confirms the root mount lacks `prjquota`; no separate 16 GiB quota
volume is attached. The guest remains aarch64. No VM, disk, worker, service,
controller, or quota state was changed.

ZZOPS status returned successfully with 11 pending cargos and no row for the
ARM route merge `0acf9314db3a9c703b307829fb02135c7a31e622` or evidence landing
`03719542af2c4873fec0dff96f8aadb78746f80a`. Its shared full-E2E worker receipt
fault belongs to other work. No task cargo is dead or queued for this change,
so nothing was resubmitted.

The review record was refreshed from the 2026-10-14 00:10 UTC readback. Its
current SHA-256 is
`1cfd28cc3cd7734456ecece8250e2aa3c623bacf8af6ddadde27878563f4bfd0`; the
schema remains
`023f3926eb43b0a80db93d4a3a81200ab739394004db979fdb0daac05612a0f1`, and the
unit template remains
`743bab7739ba40bc7ac5524ddcd18baad4a3164eb08409d4b618e16453fec4c3`. The
candidate worker release hash still verifies as
`5e795a4f7ec6ed608543437014a4bd2bb9c3764c0932d1e5d33cf0649450623c` across
262 files. The review schema and `systemd-analyze verify` both pass; the
record remains `deployable=false`.
