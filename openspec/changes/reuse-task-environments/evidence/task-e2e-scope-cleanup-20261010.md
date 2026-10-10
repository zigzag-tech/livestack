# Selected task-E2E scope check: post-run integrity failure

Recorded 2026-10-10. This was one explicitly selected task-E2E assertion,
`fleet-workload.task-environment-task-e2e-scope`; no full/coalesced E2E or
publishing job was started for this evidence.

## Assertion and worker result

- Job: `a35adeaffd7b423ab2e83c4fe72a1adc`
- Attempt: `1b30df370dc74e1a83a6b32118496649`, worker `zz-joe-e2e-1`
- Environment handle: `06318b61f53c4b3ca5cb7dc620b5702f`, generation 13
- Handler release: `e239fc504e0406963dd0fa9fda7c64de27c99fa1788ee273978af054f3fc009f`
- The selected assertion passed, and the command manifest reported a passing
  partial run. The worker then rejected parking the environment because it
  found an undeclared file under retained source. The attempt exited 0, but the
  overall job was classified as infrastructure and the environment was marked
  `rebuild_required`.
- The deployed worker release reported only
  `handler created an undeclared retained source file`, without the path. The
  authority queued a retry; the owner canceled that retry rather than spending
  another long compile on unchanged code.

This is not an assertion failure. It leaves task-environment cleanup
unverified for this run. The exact unexpected path is not known from the old
worker error.

## Diagnostic improvement and rollout state

Livestack commit `07d233d3` adds a bounded, quoted relative path to that
integrity error. The focused local regression passed (1 test). Worker release
`livestack-07d233d3` was built and staged on `zz-joe`; the staged copy
verified IDENTICAL to the local build (258 files, content hash
`7776b63e2a77b5a3e1b602bd6200ff73e51af34496d12c58f5063b80736c514f`). It
has not been activated. At the last read, worker 1 had accepted another
ZZOPS-owned full test, so its service was left untouched. Activate the staged
release only after the worker is idle, then retry this same single assertion
and record the reported path before fixing the source/cache declaration.
