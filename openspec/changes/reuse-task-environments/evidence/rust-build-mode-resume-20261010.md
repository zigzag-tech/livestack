# Benchday Rust debug/release profile reuse — 2026-10-10

Four admitted Benchday `build cli` requests used one parked Rust environment
on the same Linux host. All used source commit
`9bc72cd18b959c752902caf2196375db386ef799`, input digest
`09e9c05075c6e4aeb1d12a875f8329ae4d7c7cf9a502cae4572c99f4c5cee70a`, and
toolchain identity `778fbc643a806f0dd123c056e66201d7e8a56a61928ec0a33171be8d6c2d5a37`.

| Job | Worker / generation | Mode | Outcome | Queue / compile (seconds) |
|---|---|---|---|---:|
| `984b1ed0016c461b8599fa1410ec82c8` | `zz-joe-e2e-1` / 35 | release | succeeded; source inventory invalidated | 1.729 / 123.413 |
| `4748b38111c34fdda77be40ecf849670` | `zz-joe-e2e-1` / 36 | debug | succeeded; compatible cache reused | 1.929 / 55.558 |
| `684d2965c4a94bb8b0cc93f0d1ffb0da` | `zz-joe-e2e-3` / 37 | debug repeat | succeeded; target retained | 1.778 / 0.196 |
| `4e3f1644669949f8814d39304092bdc0` | `zz-joe-e2e-3` / 38 | release repeat | succeeded; target retained | 0.632 / 0.233 |

The same handle, compatibility identity, and `cargo-home` / `cargo-target`
cache identities were used throughout. Logs showed the requested Cargo `dev`
and optimized `release` profiles. After cleanup, the authority reported the
environment parked at generation 38 with 4,071,190,528 bytes retained and no
attempt compute allocation.

The warm repeat compile phases were under 0.24 seconds, but each request still
queued separately. This is phase evidence, not a controlled full-request
savings estimate. Because cache compatibility stayed identical across profile
changes, the sequence verifies profile-specific output freshness and warm
repeats; it does not prove a separate build-mode compatibility invalidation.
Toolchain/ABI invalidation and installed rollout acceptance remain open.
Full receipt, artifact digests, and limitations are in the Benchday companion
`evidence/rust-build-mode-resume-20261010.md`.

No full/coalesced E2E or publishing job ran.
