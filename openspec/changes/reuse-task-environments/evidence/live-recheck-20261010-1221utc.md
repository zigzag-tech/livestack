# Live recheck — 2026-10-10 12:21 UTC

## Rust environment receipt

The current-source `check rust` job
`40a26d21917e4baab2389c80ca276bed` succeeded on `zz-joe-e2e-1` at generation
22 using the existing handle. Both `cargo-home` and `cargo-target` were reused;
the source mirror updated incrementally, and the environment parked after
cleanup. The previous same-handle compiler run was on `zz-joe-e2e-2`, so this
also confirms reuse across worker changes on the same physical host. The
compiler run does not close the remaining Flutter Dart/native, symlink,
lockfile, toolchain or ABI invalidation cases.

## Installed worker and authority releases

Read-only systemd inspection on `zz-joe` found both `livestack-workload-worker`
and `livestack-workload-worker-2` using
`~/.local/share/livestack-workload-releases/livestack-377bb4e4/node-py`.
Building the worker release from current Livestack main
`4f282ce0b8ecfac36ee2e6d08a5f6f1b2165508f` produced 258 files and content
hash `8917b440ac5c11055a46f396ed75d290df5e12de2c508faa5bb5ab83534cbba3`.

Read-only release verification against the deployed `377bb4e4` directory
reported content hash
`7cf90d52f10d4e68e9442aa493c8f8f67d0ec2a6c66526893017f9dd0d05d2c2` across
246 files: 12 files occur only in the current candidate, none only in the
deployed release, and 6 files differ. Each differing deployed file was
attributed to a commit on current `origin/main`; no `HAND-EDIT` was reported.

Read-only systemd inspection on the authority host found the active unit uses
`livestack-486cf6e5/node-py` and its `_deps` directory. No authority, worker,
configuration or service was changed. The worker candidate is local only; the
authority and worker rollout, representative canary, rollback readback and
consumer-default activation remain open. No full/coalesced E2E or publishing
was run.
