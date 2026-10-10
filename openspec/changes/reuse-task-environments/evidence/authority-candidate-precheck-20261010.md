# Authority release candidate precheck

Checked 2026-10-10 at 19:31 UTC while preparing the remaining live rollout.

## Live release and candidate

The authority on `100.64.0.18` is active on
`/home/ubuntu/.local/share/livestack-workload-releases/livestack-486cf6e5/node-py`
with `_deps` on its Python path. The host reports Python 3.14.4.

Built a disposable candidate from `origin/main` at commit
`b7f3151e19289dc9a4bd9e2e77efd78d401dcae8` using
`node-py/scripts/build-worker-release.py`. Its code manifest is 258 files with
content hash
`2f77218e7eb169e971eea4ea623e32601c2a3575012ac005f29528128ccfb564`. Copied
the dependency directory from the currently active authority release into the
candidate so the check uses the same installed runtime packages. The candidate
tree including those dependencies hashes to
`38bc8918f1f6effc7b74325fc6f554972ba62aff669e40eb92488e3bdf30ab71`.

`tools/check-authority-release.py` passed all four stages—static, boot, worker
registration, and job round trip—using local Python 3.14.7. A direct check of
the source checkout without a packaged `_deps` directory failed at import time
because local system Python does not include Pydantic; the packaged candidate
passed with the active authority's dependencies.

## Rollout remains pending

This was a temporary local candidate only. No live service or worker was
changed. At the same time, ZZOPS still reported the coalesced full-suite train
as dispatched, and admitted compiler work was active on the task-capable
workers. The authority restart requires an idle window with no running or
cleanup attempts. The candidate still needs the normal fenced rollout, live
capability and cleanup readback, a representative canary, and rollback proof.

## Current-origin recheck — 2026-10-10 22:58 UTC

Refreshed the candidate from Livestack `origin/main` at
`23419ff00ffb1f8bcd5ec829a45b5706a25d6f29`. Its code manifest contains 258
files with content hash
`93fcb848aed039930378494811f662a694800e4da2af476c00cd88dee73e2f54`. The
active authority dependency directory was copied into the candidate on
`100.64.0.18`; local and staged candidate trees then matched at 409 files with
hash `da2bc5dfd3b3cf841aaf514ec0e04075b1ed19b10080816b4c8fd756c3861389`.
`tools/check-authority-release.py` passed static analysis, throwaway boot,
worker registration, and a job round trip using that candidate.

The live authority still runs `livestack-486cf6e5`. No restart was attempted:
the ZZOPS fence is open, and unrelated full-suite and compiler work is still
active on the workers. The candidate remains staged pending a quiet, fenced
window, followed by live readback, canary, and rollback proof.
