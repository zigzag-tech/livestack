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


## Follow-up, 2026-10-10: fix and live confirmation

The exact generated file was `scripts/__pycache__/zzops_runtime.cpython-313.pyc`.
Two Benchday-only environment changes did not prevent it: Livestack constructs a
handler's environment from its per-worker config, so service-level environment
settings were not inherited by the attempt. The fix is at that boundary.

- Benchday commits `ef4efa6ee` and `d17bb3aa8` also force Python to skip
  bytecode in E2E child processes and the compilation guard. Their focused tests
  pass, but they did not cover every process launched before the E2E assertion.
- Livestack commit `185edbf7` now sets `PYTHONDONTWRITEBYTECODE=1` for
  `task_e2e` attempts in `_attempt_env`, before the handler starts. Its focused
  worker test passed (1 passed).
- Worker release `livestack-185edbf7` was built from that commit and staged on
  `zz-joe`; all 258 files match the build (SHA-256
  `2f77218e7eb169e971eea4ea623e32601c2a3575012ac005f29528128ccfb564`).
  `zz-joe-e2e-1` was drained with generation checking, allowed its active Rust
  check to finish, restarted on the new release, and re-enabled at generation
  11. The unrelated full/coalesced run on `zz-joe-e2e-2` was left running.
- Live single-check job `cdb79f367f4044deaf74ddd270033fb5`, attempt
  `117ea80482454b10a0358e849ec8f9de`, ran on `zz-joe-e2e-1` with source commit
  `d17bb3aa8e4470311d62ff3f41650d10aa647e31` and the same environment handle
  `35b5c6ff9d4441b987ff2d86b7e901d4`. The named assertion passed, the handler
  exited 0, teardown was clean, no integrity error was reported, and the
  environment parked at generation 4 with 2,296,668,160 bytes retained.
- This verification rebuilt the environment after the earlier integrity
  failures (`reuse_outcome=rebuilt`); it is cleanup evidence, not a warm-cache
  speedup measurement. Its phases were 1.211 s queued, 14.421 s source
  materialization, 37.242 s dependencies, 39.193 s compile, 272.457 s test,
  and 0.006 s cleanup. A same-source warm repeat (`63356cce204f4c9aaa29e917c9ea611d`)
  remained queued with zero attempts while both E2E workers were occupied and
  was canceled by its owner. Warm-cache reuse after this fix remains unmeasured;
  the passing run proves cleanup and parking only.

No full/coalesced E2E or publishing job was run for this verification.
