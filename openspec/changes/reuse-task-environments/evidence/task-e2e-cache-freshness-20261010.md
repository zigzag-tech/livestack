# Selected task-E2E cache and source-freshness check — 2026-10-10

One explicitly selected assertion passed on `zz-joe-e2e-2`:
`fleet-workload.task-environment-source-and-cache-freshness` (1 of 931 known
checks). This uses the task-E2E handler, not the full/coalesced train.

| Field | Value |
|---|---|
| Job / attempt | `d89d056b7dc14474a8de861b4c295eab` / `6986cd48744e460ab8d2a97e4f8f7555` |
| Environment / generation | `06318b61f53c4b3ca5cb7dc620b5702f` / 11 |
| Handler release | `e239fc504e0406963dd0fa9fda7c64de27c99fa1788ee273983af054f3fc009f` |
| Source commit / tree | `18ddb97a1d22020e0c2a686f27e48458383b1fd5` / `4012cfaa3cb862920e92fc3ec6ab811e0ed44ab7` |
| Input digest | `993f60f7e6d4bf6ec25f1928b303beb4c0834be55826043f870e34f1a4ff8239` |
| Source manifest | `78e5520aa48eaa132e568c933ed48cecba006619a18028a287047dec5b224a2b` |
| Outcome | PASS; selected assertion completed in 1.572 s; disposable teardown clean |
| Environment receipt | `reused`, `source_updated_incrementally`, `parked`, 2,296,446,976 bytes retained |

All 16 environment receipt cache components reported `reused`. The result
artifact separately lists 15 dependency package directories; the worker
receipt also counts the task-E2E scratch workspace. Cleanup took 0.006 s and
left the environment parked, so its execution resources were released while
the bounded disk state remained.

| Queue | Transfer | Source | Dependencies | Compile | Test | Execution | Cleanup |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.574 s | 5.969 s | 18.616 s | 0 s | 645.113 s | 352.702 s | 1,027.839 s | 0.006 s |

Peak memory was 8,589,971,456 bytes and non-reclaimable peak was 4,425,547,536
bytes; no OOM event occurred. The assertion checked captured-source
verification, edits/deletions, compatible npm-cache reuse, lockfile-change
invalidation, and refusal to treat old generated output as a fresh build.

Artifact digests: command log
`92e7db9f1a3bb9ab1be2592f2d077080616f5363b79aedeef3fd8e0a460b1910`,
preparation `f9bfa5dbf439dd4661cfcedd546df1d702dd737fa0e3dfca8bee872947e60d30`,
task result `de48b5964c0d44748b9531387c29b999cb4121ebf29a927528ec8559486c11a6`.

This is a live correctness and reuse sample, not a cold-versus-warm savings
comparison. It does not discharge or replace the independent ZZOPS full-suite
gate.
