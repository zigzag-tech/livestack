# Post-activation capability, canary and rollout readback

Read-only authenticated snapshot at 2026-10-11T01:56:50Z. No worker, rollout
spec, quota, authority, or automatic-selection setting was changed.

## Capability and current fleet

The caller capability response reports environment API version 1. Environment
handlers allowed are Flutter check, Rust check, and task-specific E2E. The
forbidden list includes dependency and full/coalesced E2E, commerce, and all
release preparation, staging, activation, Android, CLI, daemon, Hub, iOS, macOS,
and web publishing handlers.

The authority roster reports 27 registered workers and 22 ready. Rust checks
are offered by the five zz-joe E2E workers. Only zz-joe-e2e-1 and
zz-joe-e2e-2 offer task-specific E2E. Both are ready and claim-enabled and
report the same task-handler label; their installed task-handler release set
includes 742328d4da0a9b94 and e239fc504e040696. The canary below selected the
742328d4... release. Workers 3 and 4 have no task-E2E handler; worker 5 has no
task-E2E handler and claims disabled.

The earlier per-worker config readback at 01:23:18Z showed workers 1–5
configured with 32 GiB per-replica, 128 GiB per-owner, and 124 GiB host
aggregate caps, plus a 1 GiB reserve; only workers 1 and 2 listed the
task-E2E profile. Handler registry generation 72 had default digest
742328d4da0a9b9424fe90ee2ae1cd7ae6db2f7155b7da506101ab372ea57331 installed
on workers 1 and 2 with no activation failures. The operator confirms no quota
or worker-profile change during authority activation. The current caller roster
does not expose those quota config fields.

The rollout controller remains in observe mode at spec generation 2. Its
latest report is generation 6086, with applied actions empty. It still reports
zz-joe-e2e-1 behind and workers 2–5 unknown. It proposes observed-only
stage/drain/wait/activate/smoke/enable steps for worker 1 alone. The targeted
unit is unit-f38a7baa, built from Livestack be1d8a42; its manifest has release
content hash 6593d104da0263001da63df76a4f7338818053f02e8026de3ccb8b744645d20b
and minimum authority c295e76a. This does not represent a completed worker
rollout. Worker 1's roster includes the current code manifest
93fcb848aed039930378494811f662a694800e4da2af476c00cd88dee73e2f54, while the
controller still marks its desired unit behind.

## Canary and parked-resource readback

The selected task-E2E job 2e1f68076649457abc4e2e59cb537d6b is succeeded. Its
attempt f2b1ef86f0954dd894f857361ec19f76 ended on zz-joe-e2e-1. The downloaded
CAS result artifact, SHA-256
7b3c36be009a45e1d04e18f10eef559d16600716526d8de63c6c166518720752, selects
only fleet-workload.task-environment-source-and-cache-freshness (1 of 938),
reports run verdict pass, and records clean teardown with no containers,
networks, or volumes. The handler exited 0 and its receipt reports reuse,
source_updated_incrementally, and 16/16 cache components reused on the existing
handle 06318b61f53c4b3ca5cb7dc620b5702f.

The current environment read returns parked generation 18 with 2,297,466,880
bytes retained and identifies this canary as its latest job. The prior direct
host read at 01:44Z matched the parked marker to the kernel project quota:
project 100481 on /dev/loop1, hard limit 2,834,337,792 bytes and usage
2,297,466,880 bytes. The attempt systemd unit was inactive with MainPID 0.
This shows attempt compute/quota was released while the disk workspace stayed
parked.

Recorded phases were queue 1.765 s, source materialization 17.131 s,
dependency preparation 0 s, compile 299.508 s, test 321.953 s, execution
662.440 s, transfer 6.925 s, and cleanup 0.007 s. The phases overlap and are
not additive. There is no paired cold run, so these measurements do not
quantify saved time. The harness also reported startupBudget failed at
150,346 ms against a 60,000 ms target; the selected assertion and run verdict
passed. Keep this fixture-startup diagnostic visible.

## Activation and rollback evidence

Operator-verified activation selected release livestack-c2930611 at
/home/ubuntu/.local/share/livestack-workload-releases/livestack-c2930611,
commit c2930611666a02a6db74877ec841fc113df4b52b. The 409-file package digest
is da2bc5dfd3b3cf841aaf514ec0e04075b1ed19b10080816b4c8fd756c3861389. The
operator reports static, boot, worker-registration, job-round-trip, and backup
integrity checks passed; backup is
/home/ubuntu/.local/state/livestack-workloads/backups/release-20261011T011930Z.
The live path was verified and 22/22 workers were ready six seconds after
restart, matching the pre-restart count. The abort-mode drain completed with
no stragglers; both application holds were released normally with force=false,
and accepted work was preserved. No worker, spec, quota, or automatic-selection
change was made during activation.

Activation succeeded, so production rollback was not exercised. A separate
disposable failure-path drill of the deployment script is recorded in
rollback-script-disposable-20261011.md. It verifies byte-exact restoration of
the previous drop-in and authority config in a temporary home; the candidate
checker was stubbed, and no production service was touched. This is not live
rollback proof. The verified backup and prior release pointer remain available
if an operator-authorized rollback is needed.

## Still open

Automatic selection remains disabled; the local schema-validated selector file
is absent. The worker unit/report is stale or unknown, and only two workers
advertise task-E2E. All five Rust-capable workers are on the same zz-joe host;
the other Flutter workers do not advertise Rust, so the current admitted pool
cannot demonstrate a real cross-ABI cache invalidation. The existing ABI unit
test changes platform.machine() with a mock and is fixture evidence only.
Broad worker and consumer-SDK rollout, compiler/ABI invalidation acceptance,
installed-handler stale-receipt/cancellation acceptance, and production
rollback acceptance remain open. No full/coalesced E2E run or publish was
requested or performed.
