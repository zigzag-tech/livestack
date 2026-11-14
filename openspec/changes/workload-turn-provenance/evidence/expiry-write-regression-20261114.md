# Expiry write regression — 2026-11-14

Exact predecessor: af73be76661dec8ba2d009391b64be8875f74df9.
Changed runtime SHA256: 3fe8a9894296fc74512238d1520f505adfc6ce4d2891e6661ad4ae4a9686edd5.
Tracked-source archive SHA256: 6d58dcb5ceaf2c45398fa336eaf0c5318cf18cbc732a35fbefb09df8388702e4.
Tests ran niced on zz-joe, in a separate private source copy; the existing full
feature run remains on its original af73 source, not this change.

## Cause and fix

The normal full feature run exposed an introduced failure in the existing
`test_steady_wait_writes_the_job_row_once_and_since_is_stable`. The whole outcome
module passed 30/30 on base0f8d55bf; af73 failed this case because every ordinary
claim poll issued a no-op provenance UPDATE. An indexed existence probe now
skips the UPDATE when no metadata has expired. Expired metadata still clears
in one set-based statement, including retained jobs; fixed query count is
independent of entity count. No placement, authority, TTL or retention waiver.

## Actual validation

- Current runtime: `pytest -q tests/test_workload_turn_provenance.py tests/test_workload_job_changes.py tests/test_harmony_router.py tests/test_outcome_causes.py::test_steady_wait_writes_the_job_row_once_and_since_is_stable`: **90 passed**, 3 dependency deprecation warnings,111.23s.
- Existing retained-job expiry, idle-sweep expiry, grant/owner/turn isolation,
  byte bounds, cursor index, migration and router controls are included.
- Negative control replaces only expiry with the old unconditional UPDATE in
  a worker-local pytest plugin. The SAME existing steady-wait test fails at
  `statements == []`, showing20 UPDATEs in1.04s. Product source bytes unchanged.
- Strict OpenSpec validation and whitespace checks pass.
- Raw logs: worker scratch `/home/ubuntu/scratch/livestack-expiry-guard-20261114/focused.log` and `negative.log`.
- Base/feature whole-module comparison: https://benchday.app/s/3C18BNN47P .

A second outcome-module failure is a separate pre-existing fixture assumption:
constant created timestamps plus random UUIDs make placement sort either job
first. Controlled opposing ID orders reproduce the same behavior on BOTH
base and feature: https://benchday.app/s/H1NHR1TJ8R . Scheduling is unchanged.

## Limits

This is scoped regression/contract evidence, not a whole-repository pass, SDK
gate, deployment or E9 acceptance. The normal af73 full suite is independent
old-head evidence; no current-head full pass is inferred. Operator rollout,
Jingway authoritative gate/pin, full Hub/runtime and actual same-turn
job/worker/Flutter proof remain pending.

## Completed native failing-set comparison

The normal predecessoraf73 full run completed exit1:87failed/2990passed/81skipped/1xfailed in2039.97s. Its exact87 failed IDs, not a handwritten discovery list, were replayed in suite order on base0f8d55bf then current38c793ca. Steward supplied the existing public HARMONY_TEST_NATIVE_HOST_ADDRESS=100.64.0.24 test prerequisite; broker/ledger defaults remained unchanged. Each phase had a1500s bound, no automatic retries. Both completed normally:

- Base:81 failed/6 passed in478.08s.
- Fixed:81 failed/6 passed in486.90s.
-80 failure IDs match. The sole candidate-only failure is the busy-worker UUID tie assumption already causally reproduced under opposing IDs on BOTH heads (linked above). The sole base-only failure is rootless success delivery; it passes on fixed source. No claim about its cause is inferred.
- The steady-poll write regression passes on BOTH after the fix; the old implementation negative control still fails.
- Exact phase receipt: https://benchday.app/s/CGHFH88TQP .
- Raw normal predecessor full receipt: https://benchday.app/s/5312PNS5MY ; exact87IDs: https://benchday.app/s/TJP30P3TT5 .

These results classify the old failing set; they are NOT a green full repository suite or a completed whole baseline suite. No unclassified candidate-only failure remains in that selected set. Current provenance/ACL/retention/router/steady-write tests passed90/90 separately. Existing native/release/transport/mirror failures remain visible; no policies, deadlines, routes, worker setup or tests were weakened to make this feature pass.
