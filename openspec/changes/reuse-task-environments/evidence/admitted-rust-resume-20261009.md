# Admitted Rust environment resume — 2026-10-09

Read-only authority readback at 16:42 UTC. No worker configuration or
environment policy was changed, and no full/coalesced E2E or publishing path
was invoked.

## Completed compiler attempt

Job 36de28bc5bc14f02a6cf9bcfc7deb716 succeeded (check cli, exit 0) on
zz-joe-e2e-2 / zz-joe, under installed handler release
584701d4f3b2bdd0c14607e10826637d373aeb2602ec474a3b30263834372f61.
Attempt c5cc621514f54c8eae0cf82383e98fc2 names decision
01M4GRAC8TC7E21Z5E3S5C853Z; the result identity agrees on job, attempt,
worker, input digest, handler release, and environment.

The attempt used handle 4636512da5084b70bc2f0a1b0f045020, profile
benchday-linux-rust-dev-v1, generation 7. The authority now reports that
handle parked with 1,579,089,920 bytes used and last outcome reused.
cargo-home and cargo-target both report reused; outcome reason is
source_updated_incrementally.

Measured phase seconds: queue 1,130.046; transfer 6.191; source materialization
15.705; dependencies 0.469; compile 32.603; execution 40.199; cleanup 0.006.
The test phase is explicitly not_applicable. Execution overlaps compile.
The run therefore demonstrates admitted compilation and retained component
reuse, not a controlled time saving; queue dominates and no same-source warm
pair or same-host cold comparison has been measured.

The authority result includes successful rust-check.json and command.log
artifacts with CAS digests
cfd38ed300006314a49a73844907cb5868a2c66c9af2347ab8daa0f982cb79d3 and
49d2a637c3d6bc0196dae5d32ef16c1ffd1ef85c930be52fb6090113e620fcc3.
The resource receipt reports 116,279,445 CPU microseconds, 2,302,930,944-byte
memory peak, 996,139,168-byte non-reclaimable peak, 167,452,672-byte disk
delta, 67 peak tasks, no OOM kill, and zero pids-max events.

## Next accepted request

Job bbbe018da9c2400c9a1774d39028feeb is accepted on the same handle with
rerun ID rust-cli-source-refresh-20261009; its captured source commit is
fcba798add0ca8a979297306e9d00397c0fd4581 and input digest is
d513685fc12f47f8125db7eeac4edeb5c4462db03ac0816f6979a79f41ceac23.
As of 16:42 UTC it remains queued without an attempt. Placement reports the
only Rust-profile worker busy, workers 1/3/4 without the Rust environment
profile, and worker 5 draining. Follow this durable job; do not duplicate it.

This readback confirms the authority's job/attempt/decision/worker and
environment-receipt join for one real compiler execution. Direct readback of
the installed worker's bounded decision-ledger row, other compiler modes,
cache invalidation controls, broad worker/SDK rollout, and cleanup/rollback
remain open.
