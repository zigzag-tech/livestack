# Task-environment benchmark follow-up — 2026-10-10

## Scope and load

Benchday's actual Flutter and Rust wrappers ran on admitted workers on the same
`zz-joe` host: Flutter on `zz-joe-e2e-2`, Rust on `zz-joe-e2e-1`. Flutter
reported Flutter 3.41.6 and Dart 3.11.4; Rust reported Cargo/rustc 1.98.1.
The Flutter receipt also reports Node 22.23.1 against an unenforced Node 24
baseline. Each invocation ended with its task environment parked, retaining
disk state after releasing attempt resources.

CPU PSI `some avg60` was 46.77 then 30.8 around the first Flutter attempt,
9.61 before the same-source repeat, and 36.82 before the Dart edit. Rust
samples before the two current `check daemon` probes were 3.85 and 0.72; PSI
`full avg60` was 0 in those samples. Other admitted work ran during this
period, so this is observational evidence rather than a controlled A/B.

## Flutter sequence

All three actual `scripts/flutter-remote.sh` jobs selected
`test/box_drawing_fills_test.dart`, used handle
`28a0cddc948b4723aca22d4eb27cf0b6`, and reported all four tests passing.

| Job / generation | Source digest | Cache result | Queue | Transfer | Source | Dependencies | Compile | Test | Execution | Cleanup | CPU core-s | Peak RAM GiB |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| `198fd8b68bfc4b418e2f1dd681a9a1e6` / 11 | `9a1be2eb…f2f74` | Flutter/native invalidated | 100.336 s | 5.638 s | 18.539 s | 9.980 s | 37.905 s | 28.174 s | 98.850 s | 0.012 s | 154.172 | 3.22 |
| `a588d240751b4cf3a9dcb59623b301cf` / 12 | `9a1be2eb…f2f74` | Flutter/native reused | 1.245 s | 0.183 s | 12.846 s | 2.230 s | 29.516 s | 31.932 s | 80.866 s | 0.008 s | 114.756 | 1.94 |
| `bffa6d089e96499986c3ac9373b2092f` / 13 | `1f324b99…59cb5` | Dart source updated incrementally; Flutter/native reused | 1.597 s | 5.935 s | 18.131 s | 1.860 s | 29.211 s | 41.173 s | 86.928 s | 0.010 s | 108.480 | 1.80 |

The same-source repeat's compile phase was 8.390 seconds (22.1%) below the
preceding attempt. The Dart edit preserved the cache component and measured
29.211 seconds of compile work. Every invocation still queued; the 99.091
second difference between the first two queues tracks worker availability and
load, not workspace reuse.

## Rust native-source control

The baseline and temporary one-file edit used parked handle
`4636512da5084b70bc2f0a1b0f045020`. Both actual
`scripts/rust-remote.sh check daemon` jobs ran on `zz-joe-e2e-1` and reused
`cargo-home` and `cargo-target`. The edit added a comment to
`daemon/src/guarded_fs.rs`; it was removed after the job completed.

| Job / generation | Queue | Transfer | Source | Dependencies | Compile | Test | Execution | Cleanup | CPU core-s | Peak RAM GiB |
|---|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|
| `895d76f59ed846859d84eb6ad7644c04` / 15 | 1.372 s | 5.797 s | 15.139 s | 0.519 s | 16.064 s | n/a | 22.189 s | 0.005 s | 22.890 | 2.93 |
| `ef0fb06400bf4245b1734cccc5f9edee` / 16 | 2.128 s | 6.113 s | 11.379 s | 0.206 s | 3.324 s | n/a | 8.995 s | 0.006 s | 10.637 | 1.03 |

The edited job's Cargo log has only `Checking benchday-daemon` followed by
`Finished ... in 3.27s`; no dependency crate was checked or compiled. The real
code-change positive control is job `9db7f40033f74cc3a724783b2541961e`:
source commit `19ae42b9f5c4fdde03609e6e0b1e091a2399fbf8` changes
`daemon/src/self_heal.rs` and `daemon/src/streams/sources.rs` relative to
`fcba798add0ca8a979297306e9d00397c0fd4581`. That `check daemon` reported
98.426 s compile, 1,484.105 s queue, and 313.516 CPU core-seconds. Host PSI
`some avg60` median was 18.52 (range 7.69–40.35). Its broad crate-graph check
is a timing-instrument positive control, not a paired savings comparison; see
`evidence/rust-stable-path-repeat-20261009.md`.

## Readout

The retained workspace/cache survived parked attempts, but every request still
queued. Queue, preparation, compile, test, and CPU-core-second data are
separate above. The Flutter compile difference is observed, not a controlled
total-time savings estimate. A separate Rust same-source pair measured compile
at 28.451 s then 0.173 s, with queue at 0.958 s then 0.673 s; see
`evidence/rust-stable-path-repeat-20261009.md`. These are workload-specific
results, not universal savings or a queue-latency guarantee. Full/coalesced
E2E and publishing were not run.
