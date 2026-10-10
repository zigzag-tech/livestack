# Live rollout recheck — 2026-10-10 04:17 UTC

## Authority and retained environments

Read-only authenticated `WorkloadClient` calls against the configured authority
report schema versions `[1, 2, 3, 4]` and environment policy version `1`.
Flutter compilation, Rust compilation, and `benchday.e2e.task.v1` are allowed.
Full E2E, dependency preparation, commerce, and all `benchday.release.*`
handlers are forbidden.

Two existing environments remain parked on `zz-joe`:

| Profile | Generation | Retained bytes | Last environment outcome |
|---|---:|---:|---|
| Rust development | 18 | 18,433,638,400 | reused |
| Flutter development | 13 | 1,245,880,320 | reused |

The authority roster currently reports task-E2E handlers on `zz-joe-e2e-1` and
`zz-joe-e2e-2`; compilation handlers are present on workers 1–4, while worker
5 is draining. This is current capability/roster evidence, not proof that every
worker has every retained-environment profile.

## Release and drain

The user authority service is active on release `livestack-d55c4c13`; its
release selector has not moved. Candidate `livestack-486cf6e5` and the
schema-validated, mode-0600 capacity candidate remain staged and inactive.
The current ZZOPS deploy fence is open.

At this read, three unrelated ZZOPS trains were dispatched and nonterminal:

- `tt_c0dbbb61-a4c5-4a15-9f10-2a52e8b90475`: two client-performance
  assertions on `zz-joe-e2e-1`, about 29 minutes elapsed, no progress sample.
- `tt_269f91e1-ef86-4dfb-9b89-7155a194c04f`: one streams assertion on
  `zz-joe-e2e-3`, about 6 minutes elapsed, no progress sample.
- `tt_bf9109d6-16b8-4dcf-bff2-d997461c0605`: full-suite completion on
  `zz-joe-e2e-4`, recently started, no progress sample.

The previous 10-minute abort-mode drains ended at 01:25Z (task environment
rollout) and 03:59Z (another task's CAS refresh); both released their holds.
No new hold, cancellation, authority restart, or configuration activation was
made while the three trains remained active.

## Acceptance checks

- Fresh `zzops train status --change 48c6d99f32a5ec93a87b6375953bec2fa93fed43`
  reports `complete=true`, `full=false`, PASS for all four changed
  task-environment assertions. The separate `976f…` merge also remains PASS
  for its runtime-freshness assertion.
- Livestack `test_task_environments.py` and `test_workload_environments.py`:
  55 passed.
- Livestack `test_workload_launch_receipt_files.py`: 9 passed, covering bounded
  receipt history, unsafe entries, unknown classes, and oversized receipts.
- Benchday retained-environment integration tests: 9 passed when run with the
  harness-equivalent expanded pinned runtime; handler-release checks: 3 passed;
  Rust wrapper unmanaged-refusal control: PASS. The integration test expects
  the expanded runtime view because its package reads sibling `schema.sql` by
  filesystem path.
- The systemd-backed Livestack worker test was attempted on this editor host,
  where `systemd-run --user` could not start the attempt. This is not a worker
  acceptance result; the existing real `zz-joe` worker evidence remains the
  relevant evidence.

## Remaining work

Complete the admitted source/cache invalidation cases and receipt controls,
then roll the authority/eligible workers/consumer SDK through a successful
abort-mode drain. Verify live quota, cleanup, handler scope, and forbidden
purpose behavior before enabling wrapper defaults. Do not archive either
change until those tasks finish.

## Follow-up — 2026-10-10 04:21 UTC

The one-assertion `streams.sheet-detail-via-hub` cargo reached terminal FAIL
(`sheet detail phases failed: streams.sheet-detail-eligible-machine`). It is
outside this change's `fleet-workload.*` assertion scope and did not alter the
PASS verdicts recorded above. Two trains remain nonterminal: the two-assertion
client-performance admission on `zz-joe-e2e-1` at 33 minutes with no progress
sample, and the full-suite completion on `zz-joe-e2e-4` at 4 minutes with no
progress sample. The fence remains open; no cancellation, new hold, or deploy
was attempted.
