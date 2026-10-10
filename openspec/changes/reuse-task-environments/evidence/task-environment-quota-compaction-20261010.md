# Live task-environment quota compaction

Observed 2026-10-10 on the admitted zz-joe workers running Livestack
`23419ff00ffb1f8bcd5ec829a45b5706a25d6f29`.

The real worker flow expands an idle environment for a job and compacts its
project quota after cleanup. Rust handle
`4636512da5084b70bc2f0a1b0f045020` was parked at generation 44 with a hard
limit of 2,983,505,920 bytes and 2,446,635,008 bytes used. Job
`69e9b870e2a54924ac9eb0c9c0ebcc3d` expanded it to 34,359,738,368 bytes while
running, then parked it at generation 45 with a 2,291,208,192-byte hard limit
and 1,754,337,280 bytes used. Its same-source repeat,
`86ec77e7713345af9e2589b58c47b03b`, reused both Cargo cache components and
parked the same handle at generation 46. The files and caches remained
available across compaction and reuse.

Four legacy parked handles also remained present after their large reservations
were compacted. Their current hard limits total 10,605,645,824 bytes
(9.88 GiB), against measured use of 8,458,162,176 bytes (7.88 GiB); previous
reservations totaled approximately 124 GiB. The task-E2E handle
`06318b61f53c4b3ca5cb7dc620b5702f` parked after cleanup with 2,297,409,536
bytes used and a 2,834,280,448-byte limit, exactly measured use plus 512 MiB.

The source regression `test_parked_quotas_compact_and_restore_without_changing_saved_files`
checks saved-file digests across compaction and resume, including while another
writer holds its lock, and verifies that compaction frees room for a fifth
environment and resume restores the 32 GiB execution quota. The live values
above confirm quota expansion and compaction on actual worker storage. This
closes Livestack task 3.7; it does not establish that an authority restart or
worker rollout is safe while other jobs are active.
