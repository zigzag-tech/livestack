## Why

FleetD still reads its advisory fleet observation through unauthenticated `GET /fleet` polling, even though Livestack now publishes the account-scoped `harmony.fleet/1` stream. Move that read to the authenticated stream so FleetD plans only after a bounded, current and complete observation.

## What Changes

- Replace FleetD's `/fleet` view request with an account-scope Hub subscription to `harmony.fleet/1`, bound to an explicitly configured account identity.
- Preserve the producer's raw host-level facts and keep `/fleet/plan` as the placement authority.
- Enforce named in-memory limits and distinguish unknown, stale, degraded and gap observations; only a complete fresh observation may proceed to planning.
- Add integration coverage for subscription identity, limits, recovery states and the absence of a `/fleet` fallback.

## Capabilities

### New Capabilities

### Modified Capabilities

- `fleet-supervision-loop`: the loop's fleet observation source becomes the authenticated account-scoped `harmony.fleet/1` stream and its non-fresh outcomes block planning with named states.

## Impact

`fleetd/src/client.ts`, `tick.ts`, `handbacks.ts`, and new stream-consumer code; FleetD unit/integration tests and setup documentation; the existing `fleet-supervision-loop` spec. The read side described in `_plans/fleetd-wovine-jev.md` §3 as `read_fleet_view` over the HTTP fleet view is stale; planning and operation routes remain unchanged. No changes to `fleet-rank`.
