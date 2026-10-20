# Worker rollout unit field audit — 2026-10-11 06:09 UTC

Read-only inspection of the current Livestack source and rollout documentation.
No manifest was built or registered, and no service or worker was changed.

## `min_authority` has no runtime enforcement in current code

`node-py/livestack_node/workloads/unit.py` validates `min_authority` only as a
nonempty string of at most 64 characters, then copies it into the content-
addressed unit. A repository-wide search of `node-py/livestack_node` found no
other consumer. The field therefore does not currently compare a worker unit
with the live authority release or prevent rollout against an older authority.
The proposed `c295e76a` value is metadata today; it does not prove compatibility
with active authority `c2930611`.

Before using this field as a safety gate, the owner must decide its intended
meaning and the code must implement and test that comparison. Do not fill the
field with a guessed release label and treat it as enforced.

## Verifier and capture fields

- `verifier` may be `null` in the unit schema. `worker-claims-and-rollout.md`
  says the worker cannot read the root-owned verifier copy, so current worker
  reports leave it null until the root helper reports it. A null verifier does
  not certify the ARM guest has a compilation launch verifier.
- `capture.size_bytes` and `cap_bytes` are build-command inputs. The unit
  builder checks the supplied values and runs a capture-size smoke check, but
  does not independently measure the intended live worker capture. The stale
  unit's values must be remeasured before building a replacement.
- The current rollout controller is report-only. Its documented status shows
  observations and `would_do`; it has no stage/activate executor or enforce
  mode. A built unit alone cannot perform deployment or rollback.

These facts refine the ARM enrollment gate in the paired design. They do not
change the ARM Rust-only permission intersection, physical-host accounting,
automatic-selection setting, or the owner-reported 3450 release preservation
constraint.
