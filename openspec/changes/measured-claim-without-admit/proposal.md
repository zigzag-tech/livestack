## Why

2026-10-11 18:40 EDT: a 4 GiB `benchday.release.hub.v1` job sat unclaimed 17 min on
zz-joe (62 GiB, 40 GiB really available): `insufficient host memory: claim 4.0 GiB >
free 0.6 GiB (available 37.5, reserve 1.0, running attempts 23.1, model servers 12.8)`.

The ledger already charges measured available memory plus each attempt's unrealised
claim, and the claim is the handler's learned peak. But the learned peak only ever
lowers a claim to `admit`, and `admit` defaults to `need`. Release ops declare only
`need` (the cgroup cap), so measurement could never lower their charge: two
`release.cli.linux` builds (measured peak 1.32 GiB, cap 3) were each charged 3 GiB,
the hub release (measured 1.98, cap 4) asked for 4. The scheduler also demanded the
full `admit` from the target, independent of the claim.

## What changes

For a job that states no memory `admit`, the claim is the handler's measured peak
x 1.25 (never above `need`), taken over succeeded attempts that ran under the SAME
declared `need` and only once there are 5 of them; otherwise `need`. A stated `admit`
behaves as before (floor). On measured hosts the scheduler is asked for the claim.

Not changed: `need` (the cap), any declaration, the model-server term, the pressure
gate. Handlers with a stated `admit` (e2e.full, compilation.image) are untouched.

## Follow-up: cores (2026-10-11)

CPU had the same shape: a job with no cpu `admit` reserved its cgroup quota, four attempts on
zz-joe reserved 8-10 cores while using ~1-1.2 each, and a release build waited on `cpu: needs 2,
0 free`. The same rule applies to cores, with a measured PEAK (the busiest 5 s window of
`cpu.stat`, new `cpu_peak_cores`), not the mean already recorded as `cpu_cores`: x1.25, quarter-core
rounding, floor 1 core, cap `need`, 5 succeeded samples under the same cpu `need`. Running attempts
are charged the same claim. The psi_some admission gate is untouched. Evidence table:
`node-py/docs/measured-resources.md`.
