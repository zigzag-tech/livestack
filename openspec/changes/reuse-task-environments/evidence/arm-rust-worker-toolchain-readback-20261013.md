# ARM Rust worker review readback — 2026-10-13

Read-only toolchain checks were run from `lappy-bellinzona` over SSH. On
`xc-mac-studio`, the guest was addressed through
`LIMA_HOME=$HOME/.lima-harmony` and `limactl shell --workdir /`; no VM, worker,
service, quota, authority, policy, or selection setting was changed.

## Toolchain identities

| Target | rustc | Cargo | Finding |
|---|---|---|---|
| x86_64 `zz-joe` | 1.98.1, commit `48a229ceaefd4985c50990b14116b6d856af0985`, date 2026-09-01, LLVM 22.1.8 | 1.98.1, commit `797e8a9bca276c1c9f9f738d2a20f484fa4eea9d`, date 2026-08-05 | Installed stable identity |
| aarch64 `xc-mac-studio-harmony` | 1.99.0, commit `b940084d7eb6a299eb4bfeb8e34901bc051e7ac4`, date 2026-09-28, LLVM 23.1.1 | 1.99.0, commit `5f94df4789f005f9a352888e8355ffc645b7ed0e`, date 2026-08-27 | Installed under `/home/ubuntu.guest/.cargo/bin`; that directory is absent from the login `PATH` |

The identities do not match. x86 rustup has stable, nightly, and 1.95.0; it
does not have 1.99.0. The required same-handle x86 → ARM → ARM → x86 ABI
control was not run. Owner must select/verify a matching Rust and Cargo
toolchain first; the code preparation did not install one.

## Candidate artifacts

- ARM-only handler package: `/tmp/taskenv-arm-rust-handler-candidate-0acf9314`,
  digest `a0f96feed8d7c4fb0febccdbc2dc9dd1eb0abc0f9887191fa34c18ab2ab7624d`.
- Livestack worker release built from commit `8e2947f2173801ecf05fd2c76d50d12c993306a2`:
  `/tmp/taskenv-arm-worker-service-candidate-8e2947f2`, SHA-256
  `4a33c9a230032fb4fb44a183b05d60ae9365cfd7346890f25f07dc11398eb45d`, 262
  files. It is a local immutable release candidate, not the unit manifest and
  not staged on the guest.
- Review config, its schema, and a separate user-unit template are in
  `node-py/deploy/arm-rust-worker/`. The record is explicitly
  `deployable=false`; null values are unverified, and the final runtime config
  has not been rendered or validated.

## Train request

The normal request `zzops train submit --app benchday --change
e504771bb44738de0561ba1f17cd01799190b118` returned `not-applicable`: the merge
changed no isolated-E2E assertion definition. No duplicate gate was created.
The global status showed another queued cargo for a different commit; it was
not this change and was left untouched.

The worker schema/handler config, quota helper, project-ID range, shared host
capacity values, verifier/capture inputs, unit manifest, authority comparison,
fresh target cleanup readback, matched toolchain, same-handle ABI sequence,
canary, and rollback acceptance remain open. Controller mode stays
observe-only and automatic selection remains disabled.
