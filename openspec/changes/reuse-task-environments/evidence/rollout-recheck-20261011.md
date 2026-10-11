# Current release and rollout recheck

Checked 2026-10-11 00:20 UTC from the Livestack task worktree and read-only
authority/worker views.

## Candidate checks

Livestack `origin/main` is `0246f0e3008ada29b7725177a9573b54b01c9183`; that
commit changes only this change's task ledger after the last package build.
The current worker package was rebuilt from it: 258 files, content hash
`93fcb848aed039930378494811f662a694800e4da2af476c00cd88dee73e2f54`. It is
byte-for-byte identical to the deployed `livestack-23419ff0` package on
`zz-joe-e2e-3`. Read-only systemd inspection shows workers 1–4 use that same
release. Worker 5 remains on `livestack-925a02fa` and has claims disabled.

The authority candidate was rebuilt from the same source, with the dependency
directory copied from the live release. Its 409-file content hash is
`da2bc5dfd3b3cf841aaf514ec0e04075b1ed19b10080816b4c8fd756c3861389`.
`tools/check-authority-release.py` passed static checks, boot, worker
registration, and a job round trip.

## Live authority and rollout

The authority still points at `livestack-486cf6e5`. Comparing its runtime
source and dependencies with the candidate found 12 current module files
missing and 6 older files; the candidate also adds its license file. This is a
real code difference, so the authority upgrade remains required.

The observe-only rollout still targets stale unit `unit-f38a7baa`, whose
manifest names `livestack-be1d8a42` with content hash
`6593d104da0263001da63df76a4f7338818053f02e8026de3ccb8b744645d20b`. It must
be rebuilt against the current release before it can describe the desired
worker state. At generation 5894 the rollout applied no actions; worker 1 was
`behind` and workers 2–5 were `unknown`.

At the same read, all `zz-joe-e2e` workers were idle except worker 5, which is
disabled. A separate ZZOPS admission was still running on
`xc-win-1-wsl-2` (job `6f5c534bd82743579cde7ce9943df36f`, attempt
`ee8c31e6f139477595edcd942ccc93ac`). The authority restart requires all
attempts and cleanup to be finished, so no authority, worker, rollout unit, or
automatic-selection setting was changed. No full-suite run or publish was
started for this task.
