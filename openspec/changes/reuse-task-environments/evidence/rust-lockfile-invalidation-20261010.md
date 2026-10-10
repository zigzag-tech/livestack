# Admitted Rust lockfile invalidation — 2026-10-10

## Worker result

Benchday's actual `scripts/rust-remote.sh check cli` request completed as job
`28c2f2f039e1481bbd63ba265b74e16c`. It used saved environment handle
`4636512da5084b70bc2f0a1b0f045020`, attempt
`9ab0c431e7e842158e2aeed785c21468`, worker `zz-joe-e2e-1`, environment
generation 26, and installed handler release
`584701d4f3b2bdd0c14607e10826637d373aeb2602ec474a3b30263834372f61`.

The request used input digest
`ee45cd449ac4bdf2d4431b96d5c6d6113caee933ece8df43fedf1042093e0233` and source
commit `85f8de545594239c78ce791ba9303ab0ba207e94` / tree
`7ebea60c38ca0fd0a5dd26de3f87849c1333da48`. A temporary comment was appended
to `cli/Cargo.lock` for the probe and restored afterward; the task worktree was
clean after completion. The uploaded archive was not separately fetched to
inspect those lockfile bytes.

The check succeeded with exit code 0. The receipt reported
`reason_code=cache_inputs_changed`; `cargo-home` and `cargo-target` were both
explicitly `invalidated`. The logical environment was reused and parked after
cleanup with 1,416,720,384 bytes retained.

| Phase | Time |
|---|---:|
| Queue | 521.266 s |
| Transfer | 5.976 s |
| Source materialization | 16.851 s |
| Dependency preparation | 2.756 s |
| Compile | 29.928 s |
| Total execution | 38.541 s |
| Cleanup | 0.006 s |

The queue duration is not a reuse saving. Artifacts: `rust-check.json`
SHA-256 `55ab790d7385c8a63ee79beb21ae8177237619b84f179db97017568cfee93a4e`;
`command.log` SHA-256
`631cde7a9c637cd1c976bf774011e353346f5c496bd63ffd8128c5820c623517`.

This verifies the admitted Rust lockfile invalidation case. Symlink and
compiler/ABI worker controls remain open; no full/coalesced E2E or publishing
was run.
