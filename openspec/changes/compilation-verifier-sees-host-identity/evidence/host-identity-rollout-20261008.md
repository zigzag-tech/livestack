# Host-identity rollout and admitted Rust check

Date: 2026-10-08

Livestack release `86c38445` (source commit
`86c384450d07cbb85bdbddfdf409c315527177f5`, verified tree SHA-256
`a1e4ee974e89eb7cfdaddf1514adaf2965567135c006188d8bb451140865e0ad`) was
rolled one worker at a time to the claim-enabled Linux compilation workers
`zz-joe-e2e-1` through `zz-joe-e2e-4`. Before each restart, the worker had no
active attempt journal and the authority reported no running attempt. Each
worker came back ready and idle, then its claim was re-enabled. `zz-joe-e2e-5`
remains claim-disabled and was not changed. The existing `deps-pydantic-2.12.5`
PYTHONPATH entry was retained on each worker.

The rollout also corrected the pre-existing Rust handler argv drift on
`zz-joe-e2e-2`: its native handler pointed at `/installed/handler/...`, which
does not exist on the host. It now points at the immutable handler bundle path
for release `4e2def85f0f3d5b0f3a11cdb286af46d4f88b747`. The handler file was
present and its SHA-256 was verified before the change. That worker was drained,
restarted idle, and re-enabled through the documented worker rollout sequence.

## Normal admitted check

From a clean Benchday `origin/main` worktree at commit
`e5df38c4e4a17ac91437bf0f6081292c34e223df`, the unpinned command
`scripts/rust-remote.sh --no-environment check daemon` created job
`0b0fe7ef5cd748bea34e4accd6719dfe`. The request selected Linux and
`x86_64-unknown-linux-gnu` only; it did not name a host. The scheduler retried
the first ended attempt and completed on `zz-joe-e2e-2`:

- attempt: `c2df6383977d405f940afeec30fb1eea`
- admitted classes: `native`, `rust`
- policy revision: `benchday-compilation-20261003-v4`
- result: `succeeded`, `exit_code=0`
- `rust-check.json`: 685 bytes, SHA-256
  `f37f550e5c41c2df68d4eaf0ba3b836ca84aaeb5bb172d9c9b2c53ab2e63f503`
- Cargo check elapsed: 84,083 ms; total Rust-check elapsed: 91,820 ms

The successful receipt shows that the worker accepted the compilation grant,
the handler passed its launch-verification calls, and Cargo completed under the
system-manager attempt. No host pin or task environment was used.

## Separate test-mode result

Job `f51b3e9e658846ceb62f31475d4aaffe` also ran unpinned on
`zz-joe-e2e-2` after the handler-path repair. Its handler passed launch
verification and Cargo ran, but `test daemon` ended `product_failure` (exit
101): 1,351 passed, 10 failed, and 8 were ignored. Failures included long Unix
socket paths (`SUN_LEN`), four private tmux-pane startup timeouts, one missing
ingress fixture path, and one filesystem-policy assertion. This test-mode
failure is recorded separately; it does not change the passing `check daemon`
receipt above.
