# Benchday-side follow-ups (for `measured-resource-declarations` and `storage-headroom-admission`)

Nothing here is required for the Livestack changes to land; each is an action in the
Benchday repo once the matching Livestack part ships. Owner: whoever lands the Benchday
side. Values for retention are set once, by the concurrent retention agent, in
`authority.json`; do not duplicate them here.

## Measured resources

1. `contracts/release-executors.json`: after the warning ships, re-read each handler's
   status for `declared_below_observed`. Keep `compile.daemon.linux.arm64` at 16 GiB
   (observed >8.17 GB; x64 ~5.8 GB). Decide per handler whether to opt into
   `resource_floor` (Livestack design 5).
2. Rename or redefine mis-scoped metrics, and declare every metric in the handler
   release manifest (`metrics: [{name, unit, measures, excludes}]`):
   - `postgresReady` is the first run milestone (~150 s, mostly dependency install):
     rename to what it measures (for example `firstMilestoneSeconds`) and add a real
     `postgresReadySeconds` if it is wanted.
   - `startupBudget` was sized from that number; re-derive it from the corrected metric.
   - `docker_cache.seconds`: consumers must read the cache-only meaning (Livestack
     `agent/docker-cache-overhead`) and use `session_seconds` for the whole attempt;
     check planning docs and dashboards quoting the old value.
3. Anywhere Benchday maps `execution lease lost` / `stopped without a result` to a
   user message, add the typed `resource_limit` cause (kind, observed, declared) and do
   not auto-retry it.
4. Add positive controls for Benchday-owned instruments to the e2e assertions (a metric
   that moves by a known amount), per Benchday rule 13.

## Storage and headroom

5. `docs/daemon-storage-bounds.md`: correct the stale 200 GiB objects bound, and
   document the effective bound as `min(cap, fraction of filesystem)` plus the headroom
   floor. **Not touched here:** the retention agent is editing this file; send them the
   corrections instead.
6. Stager (`xc-tower-stager`) worker config: `disk_reserve_bytes` (64 GiB) exceeds the
   free space; choose a reserve below free space or move the staging filesystem. Until
   then the roster will show `reserve_exceeds_free`.
7. Release references: confirm the rule (`keep_newest`, `ttl_seconds`) for
   `benchday.release.*` with the retention agent (130 references, ~20 GiB never expire
   today).
8. `docs/e2e-admission-capacity.md` and the `worker-cpu-admission` references: change the
   tower worker's `cpu_admission` to `psi_some` (or `runqueue`) once Livestack ships it;
   keep the documented note that `full` PSI reads 0 on this kernel.
9. Surface the new status fields (effective bound, free state, refusal `reason_code`) in
   `zzops` / Benchday status output so the 20-minute silent stall cannot recur.
