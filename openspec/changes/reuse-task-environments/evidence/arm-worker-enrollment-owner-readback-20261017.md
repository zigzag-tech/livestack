# ARM Rust worker enrollment and remaining gates — 2026-10-17

## Source and scope

The infrastructure owner supplied the enrollment and storage status in the
conversation on 2026-10-17. This agent then performed the read-only checks
listed below against the Mac guest and the zz-joe host. No worker job, service
restart, policy change, quota change, authority write, or claim enable was
performed. The update supersedes the 2026-10-13 statements below that the
quota volume was not attached, the handler was not active, and no ARM worker
existed.

## Owner-reported live facts

- Worker principal `xc-mac-studio-harmony-rust` exists on guest
  `xc-mac-studio-harmony`, with physical host ID `xc-mac-studio`.
- It was added by SIGHUP reload. Its worker claim is **OFF**; its roster state
  is IDLE; it reports exactly one handler, the ARM Rust-check handler.
- Handler `benchday.compilation.rust-check.aarch64.v1` (release digest prefix
  `a0f96fee`) is staged and active at registry generation 149. The x86_64
  default was left unchanged. The owner reports verifier slot
  `harmony-compilation-298dbc5654a35b3c`.
- Worker unit is running. Its identity token exists only in mode-0600 guest
  configuration; no credential bytes were supplied or copied here.
- The guest has a separate 8 GiB workspace and 16 GiB ext4 `prjquota`
  environment volume. The quota helper is installed and its `EDQUOT` positive
  control passed. The exact live mount path, project-ID range, configured
  maximum/reserve, helper digest, and post-restart mount readback were not
  included in this report and remain unverified here.
- The ARM worker alone is pinned to Rust/Cargo 1.98.1, matching the zz-joe
  compiler commits; the guest default toolchain and x86 worker were unchanged.
  The exact bounded ARM and x86 `rustc -vV` / `cargo -Vv` receipts have not yet
  been attached to this change.
- No automatic environment selection change was reported.

## Direct read-only readback

- The ARM worker service is `active`. Its private config file has mode `0600`;
  no token or grant content was read. The config names worker
  `xc-mac-studio-harmony-rust`, ARM target
  `aarch64-unknown-linux-gnu`, and exactly the handler
  `benchday.compilation.rust-check.aarch64.v1`. It points to the handler script
  under the captured handler release directory for source commit
  `0acf9314db3a9c703b307829fb02135c7a31e622`.
- The loaded service has no systemd drop-ins. The unit file SHA-256 is
  `ab4eaa2aebaeef8fe9dcddb01fa82f2237a395db390a674c794ecdbe5a0a213a`.
  `ExecStart` runs the Python interpreter from release
  `30a89636e97ca8481d31502af06494d24435dfcc`, while `PYTHONPATH` selects
  Livestack code under `livestack-cfdc8d30/node-py`. Those are separate loaded
  paths; the missing immutable unit manifest must bind and explain them before
  this is treated as a reviewed deployment.
- The worker-specific probe sets
  `RUSTUP_TOOLCHAIN=1.98.1-aarch64-unknown-linux-gnu`. Under that exact
  environment, direct `rustc -vV` reports release 1.98.1, commit
  `48a229ceaefd4985c50990b14116b6d856af0985`, date 2026-09-01, host
  `aarch64-unknown-linux-gnu`, LLVM 22.1.8. `cargo -Vv` reports release 1.98.1,
  commit `797e8a9bca276c1c9f9f738d2a20f484fa4eea9d`, date 2026-08-05, host
  `aarch64-unknown-linux-gnu`; the installed Rust target list contains
  `aarch64-unknown-linux-gnu`.
- The default ARM guest toolchain remains Rust/Cargo 1.99.0, confirming the
  1.98.1 selection is scoped to the ARM worker's configured probe.
- Direct `rustc -vV` / `cargo -Vv` on zz-joe report the same release, commits,
  dates and LLVM version, with host `x86_64-unknown-linux-gnu`. This is a host
  shell readback, not yet a receipt from the selected admitted x86 worker.
  Exact compiler identities now match across the readbacks, but the same-handle
  four-job sequence remains unrun.
- `findmnt` reports `/dev/loop1`, ext4, mounted with `prjquota` at the configured
  environment root. The root-only quota config has mode `0600`, names that
  mount root, sets 15 GiB (`16106127360`) maximum, and allocates project IDs
  100000–100999. The worker config retains a 1 GiB reserve. The owner-reported
  `EDQUOT` positive control was not repeated.
- The worker config reports CPU 8, memory 15,032,385,536 bytes, disk
  8,350,298,112 bytes; this is a per-worker report. The physical-host aggregate
  was not independently read and must remain the existing `xc-mac-studio`
  budget as the owner specified.

## Independent local checks in this continuation

- Benchday source at `2c9b33487`:
  `scripts/test_submit_rust_check.py` and
  `scripts/test_task_environment_handler_release.py` passed 25/25 using a
  private loopback authority, Python 3.13.15, and the captured runtime archive
  SHA-256 `1189a8a50ee0ee87d28642d5c97c4aa25f141899579cfbef797910c5b831e0bb`.
  These tests cover explicit ARM request routing, x86 default compatibility,
  toolchain-receipt validation, and immutable handler-release identity. They
  did not submit a live job or exercise the enrolled worker. Python emitted
  nonfatal `ResourceWarning`s for SQLite connections and sockets during test
  cleanup; the assertions completed with `OK`.
- Livestack fixture tests `test_machine_abi_change_invalidates_cache_before_reuse`
  and `test_profile_probe_refresh_invalidates_cache_after_toolchain_replacement`
  passed 2/2. They prove local compatibility invalidation behavior, not the
  live cross-architecture handle sequence.

## Gates still open

1. Apply/read back the authority `compilation_handlers` and policy mapping for
   this handler through the supported authority reload/restart procedure.
2. Install/read back the authorized caller grants for the ARM handler.
3. Build and read back the exact separate unit/config manifest. The previously
   owner-approved `min_authority` metadata value is `c2930611`; the current unit
   code stores this value but does not enforce it. Do not claim runtime
   enforcement. The exact manifest ID/digest, verifier digest, measured source
   capture size/cap, and installed config/unit hashes are still missing.
4. After those gates are explicitly decided, obtain fresh attempt/cleanup
   readbacks, then run one same-handle x86_64 → aarch64 → aarch64 → x86_64
   sequence with identical captured source, lockfile, options, and exact
   compiler identities. The owner-reported matching toolchain versions alone
   do not constitute the required admitted receipts or sequence.

Until the owner resolves those gates, keep `xc-mac-studio-harmony-rust` claim
OFF, do not submit an ARM job, and keep automatic selection disabled. No
authority, worker, policy, quota, or selection setting was changed by the
independent local checks recorded here.
