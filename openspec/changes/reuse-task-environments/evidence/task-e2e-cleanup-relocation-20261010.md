# Benchday selected task-E2E cleanup/relocation check — 2026-10-10

One exact assertion was run through the admitted task-E2E handler:
`fleet-workload.task-environment-cleanup-and-relocation`. No full/coalesced
E2E or publishing job ran.

The first job (`b8d5528e001a47b19c231548fe32e997`) used Benchday source
`9bc72cd18b959c752902caf2196375db386ef799`. It failed before the selected
assertion because E2E image preparation ran the Rust CPU-replay test without
its required `HARMONY_OUTPUT` directory. This was fixed later in Benchday
commit `c47a409cf`; the old job's `test` phase was zero, so it is not evidence
against the task-environment assertion.

The retry (`2849bfdd39df45b89239fd8f7f6fe25d`, attempt
`16b204d8ed7947f288f599769d67192c`) used Benchday source
`3b00ae213bc42aec92bd9bd56587c3daa43df26e` and passed the single selected
assertion. All 16 declared task-environment cache components were reused.
The attempt ran on `zz-joe-e2e-1`; its environment was parked at generation 6
after cleanup, with 2,296,938,496 bytes retained and no compute allocation.
The result reports clean teardown with no containers, networks or volumes.

Measured phases were 34.545 seconds queue, 597.357 seconds execution,
271.231 seconds compilation/preparation, 296.229 seconds test and 0.007
seconds cleanup. This confirms queueing remains per request and is not removed
by retaining workspace state. The assertion exercises relocation and expiry
through its isolated integration fixture; it does not prove a production
cross-host rollout. Toolchain/ABI invalidation and the authority/worker rollout
remain open. Full receipts and artifact digests are in the Benchday companion
`evidence/task-e2e-cleanup-relocation-20261010.md`.
