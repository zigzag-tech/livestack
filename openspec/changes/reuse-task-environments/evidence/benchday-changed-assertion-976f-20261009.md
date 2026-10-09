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
