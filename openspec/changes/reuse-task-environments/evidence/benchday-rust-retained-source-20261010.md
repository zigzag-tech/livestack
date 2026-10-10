# Benchday Rust retained-source acceptance — 2026-10-10

Benchday submitted three admitted Rust `check cli` jobs against one saved
environment on `zz-joe`. The authority returned successful outcomes and
parked receipts at generations 32, 33 and 34. Workers 3 and 4 both ran the
same environment on that host. The detailed caller evidence, including the
Flutter Dart-edit pair, is in Benchday's
`reuse-task-environments/evidence/rust-native-source-resume-20261010.md` and
`flutter-retained-cache-resume-20261010.md`.

The first Rust job observed a changed source-path inventory and cleared the
Cargo target directory before compiling. The next job restored the original
source content with the same path inventory; the target cache was retained and
`cargo check` passed incrementally. A subsequent optimized `--release check`
also passed using the same cache identities. The release-profile log shows
Cargo building optimized artifacts. All attempts ended with the environment
parked and compute released.

This is real caller/compiler evidence for source refresh, same-host worker
handoff and a second build profile. It is not a controlled cold/warm benchmark;
the compile phases were 30.353 s, 2.635 s and 48.239 s, while the optimized
request waited 82.651 s in queue. Toolchain/ABI invalidation, stale receipt
rejection, canceled-descendant cleanup and broad live rollout remain open.
