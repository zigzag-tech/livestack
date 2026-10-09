# Benchday changed-assertion gate reattachment — 2026-10-09

A fresh read-only `zzops train status --app benchday --change
976f344600eea42213ff91d4f2fab317e9af4965` returned a complete change-bound
PASS. The derived scope contains exactly
`fleet-workload.task-environment-runtime-freshness`; its assertion verdict is
`pass`, with `complete=true` and `full=false`.

The producing train is `tt_aaff49f4-15b2-4595-98a3-5ee72977aebd`, producing
commit `ed57136deaf7a5955776df796ad3b85bebd6c168`. The current main at readback
was `6782fa87f1e1b652fb56464360f9b0064abe5ea0`; status also reported
`dirty=true` because the named merge is not the current main tip. This result
comes from the explicit `change_gate` projection for the named merge, not the
older global `status.gate` field.

This closes the named merge's changed-assertion acceptance only. It is not a
full-suite verdict and does not complete the live worker/authority rollout,
rollback proof, admitted Flutter benchmark, or archive prerequisites.

A separate current `status --change` read for earlier merge
`48c6d99f32a5ec93a87b6375953bec2fa93fed43` returned `missing` with
`No complete change-bound evidence`. The PASS above is for `976f...` only and
does not resolve that earlier projection; keep the companion acceptance task
open.

## Reattachment after ZZOPS coordinator restarts — 2026-10-09

After the coordinator restarts, a fresh submission for merge
`976f344600eea42213ff91d4f2fab317e9af4965` created admission cargo
`tt_3b2860e3-8a46-4bb6-8518-53b92cd33ec4`. Admission returned `pass` with
`partial=true`. Its changed assertion is still
`fleet-workload.task-environment-runtime-freshness`.

The existing durable completion cargo
`tt_8713494a-9a06-46b1-9d5d-aaf25afead67` then settled after two attempts as
an infrastructure failure, with no retained assertion result. The last named
execution was job `08e7e67103694877823843cac8c6f86e`, attempt
`2929db5f88804e0fa8fd5b71fdc47360`, on `zz-joe-e2e-1`; its worker reported an
infrastructure outcome without a reason. A fresh `status --change 976f...`
therefore reports no complete change-bound evidence. This does not replace or
contradict the earlier recorded PASS for this merge; it is a later attempt
without an assertion verdict.

No full/coalesced E2E or publishing action was started for this reattachment.
The companion acceptance remains open while the infrastructure cause and
change-bound projection are unresolved. Do not resubmit unchanged work until
there is actionable failure evidence or the worker capacity condition changes.

## Current ZZOPS status — 2026-10-09

A subsequent read-only `zzops train status --change 976f...` still exits 3 with
`No complete change-bound evidence`. The recent-failure ledger now shows four
infrastructure outcomes for the same derived assertion across two workers:
completion jobs `08e7e67103694877823843cac8c6f86e` on `zz-joe-e2e-1` and
`95058affe97a459d90c15fdca29c77b7` on `xc-win-1-wsl-2`, plus admission jobs
`5f9c9e49f3b04c27b9cd1e3db59aeb2e` and
`15e865fc180149e0b7a137c76cb4c2d2` on `xc-win-1-wsl-2`. Each worker declared
an infrastructure outcome without an authority reason; none retained an
assertion verdict.

At this read, the scheduler reported no queued cargo, while a full completion
train and an unrelated admission were dispatched. The repeated cross-worker
infrastructure failures provide no product assertion evidence and no actionable
source fix.

The earlier complete change-bound PASS for merge `976f...` remains the
discharging result: under Benchday's `pass-is-final` policy, the first complete
PASS is final and a later infrastructure outcome cannot revoke it. The fresh
`status --change` response of `missing` is inconsistent with that retained PASS
and is recorded as a status-projection defect, not a new assertion verdict.
Do not submit again solely to refresh the projection. This does not complete the
paired task's separate benchmark, rollout, or rollback requirements.
