# Flutter development handle reattachment — 2026-10-10

## Terminal result

Benchday's admitted Flutter request
`198fd8b68bfc4b418e2f1dd681a9a1e6` completed with outcome `succeeded` and exit
code 0. Attempt `c08fea08f0d54e10adbfebd7432f4817` ended on
`zz-joe-e2e-2` at environment generation 11. It used the existing handle
`28a0cddc948b4723aca22d4eb27cf0b6`, which remained parked with
1,154,777,088 bytes after the attempt.

The environment receipt says `reuse_outcome=reused`, while its
`flutter-native` cache component says `invalidated` with reason
`cache_inputs_changed`. This demonstrates reattachment to retained workspace
state and attempt cleanup. It does not demonstrate a warm Flutter/native cache
hit or a compile-time saving.

| Phase | Seconds |
|---|---:|
| Queue | 100.336 |
| Transfer | 5.638 |
| Source materialization | 18.539 |
| Dependencies | 9.980 |
| Compile | 37.905 |
| Test | 28.174 |
| Execution | 98.850 |
| Cleanup | 0.012 |

Resources report 154.17 CPU seconds, peak memory of 3,452,518,400 bytes, and
no OOM kill. Host CPU pressure was elevated during the observation. Queue time
remains for each invocation; these timings are not a controlled comparison.

Input/source digest:
`9a1be2eb5bf9f5bc0c18bf1bdd042dd677d1ed1f12195cca5dbb68a9c34f2f74`.
Artifact digests: `command.log`
`d09a27e3910785ae3bbc27c35ad5343d462bf3506c65a4309a8c30af0576f1b6`;
`flutter-check.json`
`be23023cc83c7578fec76b47abf3f441e5f1eddfb30d4e086e383cec7f7e8509`.

This is Benchday caller evidence only; it does not complete Livestack's
same-source cache-repeat, source-invalidation, worker-rollout, or rollback
acceptance.

## Same-source repeat — 2026-10-10

The second wrapper invocation used a new rerun ID with the same source/input
digest and test selection. Job `a588d240751b4cf3a9dcb59623b301cf` succeeded on
`zz-joe-e2e-2`, attempt `b981cbea7b4b4c0c8e0827e1c114ea16`, at generation 12.
It reused the `flutter-native` cache component and left the same handle parked
at 1,200,328,704 bytes. The wrapper reported all four selected tests passed.

| Job | Cache result | Queue | Source | Dependencies | Compile | Test | Execution |
|---|---|---:|---:|---:|---:|---:|---:|
| `198fd8b68bfc4b418e2f1dd681a9a1e6` | component invalidated | 100.336 s | 18.539 s | 9.980 s | 37.905 s | 28.174 s | 98.850 s |
| `a588d240751b4cf3a9dcb59623b301cf` | component reused | 1.245 s | 12.846 s | 2.230 s | 29.516 s | 31.932 s | 80.866 s |

Compile was 8.390 seconds (22.1%) lower and queue was 99.091 seconds lower
on the repeat, while host CPU pressure also fell from elevated samples around
the first run (`46.77`, then `30.8`) to `some avg60=9.61` before the repeat.
Treat the pair as observed timings, not a controlled estimate of reuse savings.
Each invocation still entered the queue. This caller evidence does not close
the alternating Dart/native benchmark, source invalidation, worker rollout,
rollback, or archive requirements.

Repeat input/source digest:
`9a1be2eb5bf9f5bc0c18bf1bdd042dd677d1ed1f12195cca5dbb68a9c34f2f74`.
Artifact digests: `command.log`
`396fcd3ba74f76dbc216ca10ab7c491bff067daa0632bd84c6e6275a6dd0afd8` and
`flutter-check.json`
`9dd0a3284406cd53ad4d609d6902dfb65fbfa6a0b4f07414a53aabb53437cd73`.
