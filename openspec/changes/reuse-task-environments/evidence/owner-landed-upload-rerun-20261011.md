# Owner-landed upload fixes rerun — 2026-10-11

Livestack task worktree is at `fbcd96f2991b4182e81a46ada4670bfad99595bd`, equal to `origin/main`. Both owner commits are present: `000e253fcce1c1e2a6f4c252e944d77675496bd9` makes an oversized artifact a terminal `ArtifactTooLarge` result, and `94128fcf17d58d0ed591a7887ee06b4c3f26328b` updates the retry test to assert the current warning behavior.

The complete worker suite was run with its pytest temporary directory under `/home/ubuntu`:

```text
.venv/bin/python -m pytest tests/test_workload_worker.py \
  --basetemp=/home/ubuntu/.cache/livestack-pytest-home-20261011-01 -q
```

Result: 76 passed, 6 skipped, 1 failed in 159.11 seconds. The only failure was `test_receipt_resources_move_by_the_known_amount_and_say_where_they_came_from`: after a 24 MiB write, its `disk_delta_bytes` reading was 0 rather than at least 21.6 MiB. `findmnt -T` identifies the test's home filesystem as Btrfs (`/dev/mapper/root[/@home]`). Keep the assertion unchanged; this run does not verify the measurement on the intended bounded ext4 worker filesystem.

Focused worker controls covering handler output bounds, terminal oversized-artifact handling, and transient upload retry passed: 5 passed, 78 deselected in 6.33 seconds. The separate transfer suite passed: 1 passed in 1.00 second. Both used distinct home-based pytest temp directories. These results cover the landed upload fixes despite the unrelated Btrfs disk-delta failure.

No source/test file, live worker, service, quota, or policy was changed during these reruns. The pre-existing dirty OpenSpec change and evidence files were preserved while fast-forwarding to `origin/main`.

## Bounded ext4 worker-filesystem rerun

To resolve the Btrfs-only disk-delta failure, the committed `node-py` source at Livestack `fbcd96f2991b4182e81a46ada4670bfad99595bd` was archived to a disposable test directory on `zz-joe`. The test used Python 3.13.5 / pytest 9.1.1. Its pytest base directory was on `/var/lib/livestack-workloads/environment-hosts/zz-joe/task-environments`, mounted from `/dev/loop1` as ext4 with `prjquota`.

- `test_receipt_resources_move_by_the_known_amount_and_say_where_they_came_from`: 1 passed in 6.14 seconds on the bounded ext4 volume.
- Full `tests/test_workload_worker.py`: 80 passed, 3 skipped in 235.75 seconds on that same volume.

This confirms the disk-delta positive control and full worker file pass on the ext4-backed worker filesystem. The separate project-quota overflow/isolation control remains the evidence recorded under task 3.1; this pytest directory did not receive its own project quota. No shared workload was submitted, and no worker service, config, quota, or policy was changed. The Btrfs result remains a host-specific measurement limitation rather than a change to the assertion threshold.

After these tests, `origin/main` advanced from `fbcd96f2991b4182e81a46ada4670bfad99595bd` to `258aaefbf0a872b32754a6ba12086d450bbeba87`. The two intervening commits do not touch the worker test, resource-measurement module, or transfer module. The task worktree was fast-forwarded after confirming no overlap with its dirty OpenSpec files. The recorded runs therefore cover the exact relevant source under test; the current task worktree also contains those fixes.

## Fresh home-based rerun — 2026-10-13

Fetched `origin/main` at Livestack `8e2947f2173801ecf05fd2c76d50d12c993306a2`; it contains both owner fixes `000e253fcce1c1e2a6f4c252e944d77675496bd9` and `94128fcf17d58d0ed591a7887ee06b4c3f26328b`. No worker source/test diff remains in this task worktree.

On Python 3.13.15 / pytest 9.1.1, the complete `node-py/tests/test_workload_worker.py` run used this home-based pytest directory:

```text
uv run --project . --extra dev --python 3.13 pytest -q tests/test_workload_worker.py --basetemp=/home/ubuntu/.cache/livestack-worker-pytest.ebpBxQ
```

It finished `77 passed, 6 skipped, 1 failed` in 151.33 seconds. The failure was `test_receipt_resources_move_by_the_known_amount_and_say_where_they_came_from`: `disk_delta_bytes` was 0 after its 24 MiB write, below its 90% positive-control bound. `findmnt -T /home/ubuntu` reports Btrfs. This is the same host-specific disk-measurement limitation documented above; the bounded-ext4 run remains separate evidence.

The landed upload-retry assertion was also rerun by itself with a home-based pytest temp directory:

```text
.venv/bin/pytest -q tests/test_workload_worker.py::test_transient_failure_during_result_upload_is_retried --basetemp=/home/ubuntu/.cache/livestack-worker-focused.y5NQtj
```

It passed in 2.19 seconds. The current full-file run therefore verifies the worker changes except for the Btrfs disk-delta measurement; no worker service, quota, or policy was changed.
