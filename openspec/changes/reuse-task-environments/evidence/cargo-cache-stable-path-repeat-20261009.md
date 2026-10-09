# Admitted Cargo fingerprint reuse after stable-path rollout — 2026-10-09

## Worker rollout

Worker source commit `377bb4e4a567a43b65b114078cadf643952231f8` was built as
an immutable 246-file candidate with content hash
`7cf90d52f10d4e68e9442aa493c8f8f67d0ec2a6c66526893017f9dd0d05d2c2` and
installed on `zz-joe-e2e-2`. The worker had been drained while idle. Its
existing task-environment mount was root-owned `1770` ext4 with project
quotas; the missing sibling `task-environment-view` was provisioned as
`ubuntu:ubuntu`, mode `0700`. The candidate and installed release were
verified identical. Only the worker's task-environment PYTHONPATH drop-in was
updated. The service was reloaded and restarted, returned active with exit
status 0 and the new release path, then was re-enabled at claims generation
11. The authority service was not restarted.

The stable view directory is a fixed source bind path beside the retained
host environment mount. Attempts still have separate systemd mount
namespaces and cleanup. This preserves Cargo's absolute source/target path
across attempts while releasing attempt CPU and memory after cleanup.

## Admitted compiler receipts

Both jobs ran `scripts/rust-remote.sh check cli` on the same host with handle
`4636512da5084b70bc2f0a1b0f045020`, profile
`benchday-linux-rust-dev-v1`, exact source commit
`fcba798add0ca8a979297306e9d00397c0fd4581`, and input digest
`d513685fc12f47f8125db7eeac4edeb5c4462db03ac0816f6979a79f41ceac23`.
Both terminal results are `succeeded`; each job's receipt says
`compatible_environment_reused`, both Cargo cache components are `reused`,
and the environment is parked after the attempt.

| Job | Generation | Attempt | Decision | Compile | Queue | Transfer | Source materialization | Cargo log |
|---|---:|---|---|---:|---:|---:|---:|---|
| `478c7dd2776a422fa6ded2d375f93a20` | 9 | `466ffd21403a47c2a9a74f3c44e8c146` | `01M4GVAX8W9KN8YHZEYC1ZPTAF` | 28.451 s | 0.958 s | 6.779 s | 14.627 s | 75 `Compiling`, 234 `Checking` |
| `bf60dffb4ee64dd086e4bdf6b8279d47` | 10 | `9006e46cc4194e4f844ecd015cc40e57` | `01M4GVFFMQVS487A2JH0W55X62` | 0.173 s | 0.673 s | 0.183 s | 14.070 s | 0 `Compiling`, 0 `Checking` |

Dependency preparation measured 0.342 s and 0.122 s; cleanup measured 0.0056
s for each; tests were not applicable. The Cargo phase decreased 28.278 s
(99.4%) for this exact same-source repeat. The repeat used 7,779,322 CPU
microseconds and peaked at 140,775,424 bytes of memory, versus 102,691,570
microseconds and 2,505,560,064 bytes for the first attempt. Both attempts
reported zero OOM kills and zero pids-max events. The environment remained
parked with 1,913,688,064 bytes retained.

Both authority job records are joined to these attempt IDs, the same worker
`zz-joe-e2e-2`, host `zz-joe`, and the placement decisions shown above. The
accepted/completed identity names handler release digest
`584701d4f3b2bdd0c14607e10826637d373aeb2602ec474a3b30263834372f61`. This is
a real Cargo fingerprint hit after the stable-path worker release; it does
not remove the per-invocation queue. Host-wide load was not sampled during
these two jobs, and this pair does not cover Dart/native edits, lock/toolchain
invalidation, cross-worker reuse, or a load-controlled alternating benchmark.

## Artifact digests

| Job | `command.log` | `rust-check.json` |
|---|---|---|
| `478c7dd2776a422fa6ded2d375f93a20` | `709cf8a19d18cc204731e82a41d9b1298cc8e41d8e42c3ab20c152d85dba84c7` | `1f41974a45344791840f22bca9c28cd558f923b7faea12cb5234c351183b6447` |
| `bf60dffb4ee64dd086e4bdf6b8279d47` | `229407f13c62c51fd52c59b9bc0d8d8e0674225455f94b8d2826a5c55f7d5ec7` | `efaed5ae84f8b59377d33588730c0a842b900e7ee4ee38b6adf5618c2b61162e` |

## Changed-source daemon check completed — 2026-10-09 18:31 UTC

This supersedes the 18:02 UTC queued observation. The durable job
9db7f40033f74cc3a724783b2541961e completed successfully on zz-joe-e2e-2;
it was submitted once and was not resubmitted. It ran check daemon against
source commit 19ae42b9f5c4fdde03609e6e0b1e091a2399fbf8, source digest
43fed7859e6eb278cdd243f50cbd8ef299235a9df490f88f4f4adf6b9b3e757c, attempt
51f2bbed87364269852555bcba4812ee, and Rust handler release
584701d4f3b2bdd0c14607e10826637d373aeb2602ec474a3b30263834372f61.

The request reused handle 4636512da5084b70bc2f0a1b0f045020. The receipt says
reuse_outcome=reused and reason_code=source_updated_incrementally. Both
cargo-home and cargo-target were reused; the parked environment advanced from
generation 10 (1,913,688,064 bytes) to generation 11 (3,749,679,104 bytes).

| Phase or resource | Result |
|---|---:|
| Queue | 1,484.105 s (about 24.7 min) |
| Transfer | 7.081 s |
| Source materialization | 16.730 s |
| Dependency preparation | 3.562 s |
| Cargo check daemon | 98.426 s |
| Worker execution | 110.452 s |
| Cleanup | 0.006 s |
| Test | not applicable |
| CPU | 313,516,097 usec |
| Peak memory | 5,432,221,696 bytes |
| Peak non-reclaimable memory | 1,762,037,424 bytes |
| Disk delta | 1,835,868,864 bytes |
| OOM kills / pids-max events | 0 / 0 |

The installed workload-decisions ledger was read directly. Admission decision
01M4GY367JS3Y6S8QQDCSB4YME chose worker 2. Completion decision
01M4GY7MQFBFYJDKA243T6S3Z1 links to that admission through parent_decision_id
and records the job, attempt, worker zz-joe-e2e-2, host zz-joe, handle,
generation 11, succeeded product outcome, and reused environment outcome.
The command artifact hashes are 901bb34508306ff7c6ebe2f4f95140c6d267046194b1ef5d2f1a76d0a0167578
(command.log) and 40f66843a9467031187ee2140678f585a81fe556ea0fcc98e2735115ba7451d4
(rust-check.json).

Host load sampling spans 17:51:23–18:19:00 UTC at a 5-second median interval:
318 samples total and 27 during this attempt. Load1 min/median/max was
12.07/14.33/18.70; CPU PSI some avg60 min/median/max was
7.69/18.52/40.35. This sample describes host conditions, not a controlled
before/after comparison. Queue dominated the run, and the changed-source
daemon result cannot be compared with the unchanged-source CLI pair as a
total wait-time saving. No full/coalesced E2E or publishing was invoked.
