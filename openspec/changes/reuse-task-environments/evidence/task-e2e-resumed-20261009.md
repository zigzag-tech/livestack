# Task-E2E resumed after slot cleanup — 2026-10-09 23:42 UTC

## Accepted Benchday request

The exact request `9e08c6cc02fa4c00b1dbddaffed763dc` was accepted earlier for
`fleet-workload.task-environment-runtime-freshness` on environment handle
`06318b61f53c4b3ca5cb7dc620b5702f`. Its original caller observer reached its
1800-second wait deadline and exited 75 with state `pending`. The observer
process is gone, but the authority still reports the same job as running; it
was not resubmitted.

At 23:42 UTC, the job attempt was
`1c15de934a3e41af98b07ea2de44c0b5` on `zz-joe-e2e-1`, with
`environment_generation=10`. The environment readback was `preparing`,
`last_outcome=reused`; the job had `result=null`. This confirms dispatch and
generation advancement, not successful runtime completion or cache/source
freshness. Continue reading this job ID through terminal completion.

## Worker and train cleanup

ZZOPS train `tt_76035e95-a06d-464b-9666-a4f159b4d18d` was an unrelated full
completion request on worker 2 with no shared-tree riders and no progress
reported after about 78 minutes. It was cancelled through ZZOPS at 23:36 UTC,
audit `tt_3c18643a-4dbb-4d6a-99d6-eb8571fb54fa`, cancelling Harmony job
`473610d2f7ce4aa0b36893be668e8d32`. The authority reported cleanup pending
and worker 2 not-ready until it reports clean.

An owner-directed repeat submit for landed merge
`48c6d99f32a5ec93a87b6375953bec2fa93fed43` returned a separate four-assertion
cargo `tt_1b5e2914-0100-427d-ad02-b5aefe9e375a`. The current merge-bound
status already reported PASS/complete/full=false, so this duplicate was
cancelled at 23:41 UTC, audit `tt_278ac3c7-a66f-40ca-9a8e-a8298ffd196d`.
Its Harmony job `6f838fe320e144099890259ccc359c19` was cancelled on worker 2
with cleanup pending. The duplicate cancellation has no test verdict and does
not replace the merge-bound PASS.

The separate worker-1 full-product train
`tt_2ef90877-ebd7-44c5-bd89-02e499167781` ended at 23:35:53 UTC with
`Harmony execution refused: Harmony result selection differs from the accepted
train execution`; its reported selection was `client-performance`. It
produced no verdict for the named hub-stop/redeploy assertion. The global
`status.gate` field was not used.

The only available local workload config was the non-admin `client.json`.
Handler-release status refused that principal; no operator config is present,
so no activation or rollback was attempted. Any required rollback needs an
authoritative generation read and `--expected-generation`. This execution
refusal is not a product assertion failure.

## Terminal result — 2026-10-09 23:48 UTC

This supersedes the running-only observation above. A direct authority read
reported job `9e08c6cc02fa4c00b1dbddaffed763dc` as `succeeded` on
`zz-joe-e2e-1`, attempt `1c15de934a3e41af98b07ea2de44c0b5`. The exact check
`fleet-workload.task-environment-runtime-freshness` passed (1 of 923 known
checks, handler exit code 0). It verified a fresh attempt-owned tmpfs
PostgreSQL database, persistence of its sentinel through a controlled SQL
failure, an empty database on the next invocation, run-scoped container and
network names, and clean teardown with no remaining containers, networks, or
volumes.

The same task-E2E handle `06318b61f53c4b3ca5cb7dc620b5702f` was reused at
generation 10 with reason `source_updated_incrementally`; all 16 cache
components report `reused`. The terminal environment is `parked` with
2,294,861,824 bytes retained. The attempt ended and cleanup took 0.006 seconds,
confirming the task retained disk state after releasing attempt resources.

| Phase | Seconds |
|---|---:|
| Queue | 1,667.505 |
| Transfer | 8.167 |
| Source materialization | 20.170 |
| Dependency preparation | 0.000 |
| Compile | 375.062 |
| Test | 322.055 |
| Execution | 733.270 |
| Cleanup | 0.006 |

The request waited about 27.8 minutes in queue. This is not evidence of queue
reduction and does not complete the alternating cold/repeat/Dart-edit/native-
edit benchmark. The exact task-E2E artifact is not full/coalesced E2E gate
evidence; no full suite or publisher was invoked.

Captured source commit/tree are
`0d62a596dbf17fed35796039f0102fb57a654595` /
`c265fc919071c020d0e0b8f3b0b13999d32619bb`; source manifest digest is
`9a2f158615bd2f95336423329485a619f180b52a1fe22ea9ee88f2a1deef1faa` and
input digest is `f650fb2fd59b8bc13fffd8c6ad06a43e893d3e3bbfc1a55df91dde586ad6cc3c`.
Artifact digests: result JSON
`a1b87d4c41f0ae61159d0644bc8bb55c360dbce14f6fdc33edc229bf80953805` (2,814
bytes), command log `d07f3b0486215716c8e73bea4a1f4622f64dfe4bae8a9855bfd17dacd5305b5f`
(254,076 bytes), and preparation JSON
`db38c1b77a252eb5410ddce26b05c7a7db624c735b493f836a70044e307592f6` (1,330
bytes).
