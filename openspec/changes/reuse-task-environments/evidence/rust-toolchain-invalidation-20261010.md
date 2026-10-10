# Rust toolchain identity invalidation

Date: 2026-10-10 23:33 UTC

An admitted Rust check changed the worker's compiler identity for an existing
task environment and exercised the installed cache invalidation path.

- Job `45644ca87cdc4611961ec39caf28401e`; attempt
  `05c79694ec4643a2886fda4c3412ca1e`; worker `zz-joe-e2e-3`.
- The same handle, `4636512da5084b70bc2f0a1b0f045020`, had existing Rust 1.98.1
  Cargo caches. A temporary worker profile probe reported Rust 1.95.0, and the
  captured source pinned Cargo to the installed 1.95.0 compiler.
- The `cli` Cargo check succeeded with exit code 0. The environment receipt
  reported `reuse_outcome=rebuilt`, `reason_code=toolchain_changed`, and marked
  both `cargo-home` and `cargo-target` invalidated.
- The changed toolchain identity was
  `778fbc643a806f0dd123c056e66201d7e8a56a61928ec0a33171be8d6c2d5a37`.
- Queue time was 327.719 s; source materialization 25.381 s; transfer 6.555 s;
  dependency setup 2.571 s; compile 32.221 s; cleanup 0.006 s. Peak memory was
  2,707,328,832 bytes. Cleanup left the environment parked at generation 47
  with 1,418,596,352 bytes retained and no attempt resources assigned.
- Worker 3 was restored from its saved configuration, restarted while drained,
  verified ready with no activation failures or temporary label, and re-enabled
  at claim generation 11.

This closes the real Rust compiler-version invalidation subcontrol. Separate
ABI and build-mode invalidation remain open. The long queue phase shows that
reusing a workspace avoids repeated compilation but does not remove scheduling
delay for each request.
